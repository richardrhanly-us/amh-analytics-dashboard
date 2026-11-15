using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;

namespace SortView.Setup.Wizard
{
    /// <summary>What happened to a request to save a copy of the setup log. Fixed codes only.</summary>
    public enum SaveCopyResult
    {
        Saved,
        NoLog,
        AlreadyExists,
        UnsafeLocation,
        Failed,
    }

    /// <summary>
    /// Creates a NEW file for the elevated installer at a path a person chose, without ever following a link.
    ///
    /// The path must be on a local drive (fixed or removable). Starting at the drive's root, every folder is opened by
    /// handle, relative to the folder above it and without following reparse points, and refused if it is one (a junction,
    /// a symbolic link, a mount point, or any other reparse point). The file is then created relative to the last folder's
    /// handle, as a new file only (an existing file -- a link included -- is never opened). A folder renamed or replaced
    /// by a link after it was checked therefore cannot redirect the write: the handles already held are what is used, never
    /// the path again. Nothing is written until the file has been created this way.
    /// </summary>
    internal static class SafeNewFile
    {
        private static readonly string[] DeviceNames =
        {
            "CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
            "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9", "CONIN$", "CONOUT$",
        };

        /// <summary>Creates the file, or returns null and says why. <paramref name="afterFoldersChecked"/> (TEST ONLY) runs
        /// once every folder is open and checked, just before the file is created. Never throws.</summary>
        internal static FileStream Create(string path, Action afterFoldersChecked, out SaveCopyResult outcome)
        {
            var held = new List<SafeFileHandle>();
            try
            {
                string[] parts = LocalParts(path);
                if (parts == null)
                {
                    outcome = SaveCopyResult.UnsafeLocation;
                    return null;
                }
                // The drive's root, then each folder below it, each opened relative to the one before.
                SafeFileHandle folder = Open(null, @"\??\" + parts[0] + @"\", Folder, out int status);
                if (folder == null)
                {
                    outcome = SaveCopyResult.Failed;
                    return null;
                }
                held.Add(folder);
                for (int i = 1; i < parts.Length - 1; i++)
                {
                    folder = Open(held[held.Count - 1], parts[i], Folder, out status);
                    if (folder == null)
                    {
                        outcome = SaveCopyResult.Failed;
                        return null;
                    }
                    held.Add(folder);
                    if (IsReparsePoint(folder))
                    {
                        outcome = SaveCopyResult.UnsafeLocation;
                        return null;
                    }
                }
                afterFoldersChecked?.Invoke();
                SafeFileHandle file = Open(held[held.Count - 1], parts[parts.Length - 1], NewFile, out status);
                if (file == null)
                {
                    outcome = status == StatusObjectNameCollision || status == StatusFileIsADirectory ? SaveCopyResult.AlreadyExists : SaveCopyResult.Failed;
                    return null;
                }
                outcome = SaveCopyResult.Saved;
                return new FileStream(file, FileAccess.Write);
            }
            catch (Exception)
            {
                outcome = SaveCopyResult.Failed;
                return null;
            }
            finally
            {
                foreach (SafeFileHandle handle in held)
                {
                    handle.Dispose();
                }
            }
        }

        /// <summary>Marks a file this class created for deletion when it is closed (by handle: never by its path).</summary>
        internal static void RemoveOnClose(FileStream stream)
        {
            try
            {
                byte delete = 1;
                SetFileInformationByHandle(stream.SafeFileHandle, FileDispositionInfo, ref delete, 1);
            }
            catch (Exception)
            {
                // Best effort: a partial copy the person can see and remove themselves.
            }
        }

        /// <summary>The drive ("C:") and each name below it, for a plain local path; null for anything else (a network or
        /// device path, a stream, a name Windows treats specially, or a drive that is not fixed or removable).</summary>
        private static string[] LocalParts(string path)
        {
            if (string.IsNullOrWhiteSpace(path) || path.Length < 4 || !char.IsLetter(path[0]) || path[1] != ':' || path[2] != '\\' ||
                path.IndexOf('/') >= 0 || path.IndexOf(':', 2) >= 0)
            {
                return null;
            }
            string full;
            try
            {
                full = Path.GetFullPath(path);
            }
            catch (Exception)
            {
                return null;
            }
            if (full.Length < 4 || !full.StartsWith(path.Substring(0, 3), StringComparison.OrdinalIgnoreCase))
            {
                return null;
            }
            DriveType type = new DriveInfo(full.Substring(0, 1)).DriveType;
            if (type != DriveType.Fixed && type != DriveType.Removable)
            {
                return null;
            }
            string[] names = full.Substring(3).Split('\\');
            foreach (string name in names)
            {
                if (name.Length == 0 || name.EndsWith(".", StringComparison.Ordinal) || name.EndsWith(" ", StringComparison.Ordinal) ||
                    name.IndexOfAny(Path.GetInvalidFileNameChars()) >= 0 || name.IndexOf(':') >= 0 ||
                    DeviceNames.Contains(name.Split('.')[0].TrimEnd(' '), StringComparer.OrdinalIgnoreCase))
                {
                    return null;
                }
            }
            return new[] { full.Substring(0, 2) }.Concat(names).ToArray();
        }

        private static bool IsReparsePoint(SafeFileHandle handle)
        {
            if (!GetFileInformationByHandleEx(handle, FileAttributeTagInfo, out AttributeTagInfo info, (uint)Marshal.SizeOf(typeof(AttributeTagInfo))))
            {
                return true; // cannot tell: treated as unsafe
            }
            return (info.FileAttributes & FileAttributeReparsePoint) != 0;
        }

        // ---- native ---------------------------------------------------------------------------------------------------

        private struct Request
        {
            public uint Access;
            public uint Attributes;
            public uint Share;
            public uint Disposition;
            public uint Options;
        }

        private const uint Synchronize = 0x00100000, Delete = 0x00010000, GenericWrite = 0x40000000;
        private const uint FileTraverse = 0x20, FileReadAttributes = 0x80;
        private const uint FileShareRead = 1, FileShareWrite = 2, FileShareDelete = 4;
        private const uint FileOpen = 1, FileCreate = 2;
        private const uint FileDirectoryFile = 0x1, FileSynchronousIoNonAlert = 0x20, FileNonDirectoryFile = 0x40, FileOpenReparsePoint = 0x200000;
        private const uint FileAttributeNormal = 0x80, FileAttributeReparsePoint = 0x400;
        private const uint ObjCaseInsensitive = 0x40;
        private const int StatusObjectNameCollision = unchecked((int)0xC0000035), StatusFileIsADirectory = unchecked((int)0xC00000BA);
        private const int FileAttributeTagInfo = 9, FileDispositionInfo = 4;

        // An existing folder, opened as itself (a reparse point is opened, never followed).
        private static readonly Request Folder = new Request
        {
            Access = FileTraverse | FileReadAttributes | Synchronize,
            Share = FileShareRead | FileShareWrite | FileShareDelete,
            Disposition = FileOpen,
            Options = FileDirectoryFile | FileSynchronousIoNonAlert | FileOpenReparsePoint,
        };

        // A new file only: if the name exists in any form (a file, a folder or a link), nothing is opened.
        private static readonly Request NewFile = new Request
        {
            Access = GenericWrite | Delete | FileReadAttributes | Synchronize,
            Attributes = FileAttributeNormal,
            Share = 0,
            Disposition = FileCreate,
            Options = FileNonDirectoryFile | FileSynchronousIoNonAlert | FileOpenReparsePoint,
        };

        private static SafeFileHandle Open(SafeFileHandle parent, string name, Request request, out int status)
        {
            IntPtr buffer = Marshal.StringToHGlobalUni(name);
            IntPtr unicode = Marshal.AllocHGlobal(Marshal.SizeOf(typeof(UnicodeString)));
            bool added = false;
            try
            {
                Marshal.StructureToPtr(new UnicodeString { Length = (ushort)(name.Length * 2), MaximumLength = (ushort)(name.Length * 2), Buffer = buffer },
                    unicode, false);
                if (parent != null)
                {
                    parent.DangerousAddRef(ref added);
                }
                var attributes = new ObjectAttributes
                {
                    Length = Marshal.SizeOf(typeof(ObjectAttributes)),
                    RootDirectory = parent?.DangerousGetHandle() ?? IntPtr.Zero,
                    ObjectName = unicode,
                    Attributes = ObjCaseInsensitive,
                };
                status = NtCreateFile(out SafeFileHandle handle, request.Access, ref attributes, out IoStatusBlock _, IntPtr.Zero,
                    request.Attributes, request.Share, request.Disposition, request.Options, IntPtr.Zero, 0);
                if (status < 0)
                {
                    handle.Dispose();
                    return null;
                }
                return handle;
            }
            finally
            {
                if (added)
                {
                    parent.DangerousRelease();
                }
                Marshal.FreeHGlobal(unicode);
                Marshal.FreeHGlobal(buffer);
            }
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct UnicodeString
        {
            public ushort Length;
            public ushort MaximumLength;
            public IntPtr Buffer;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct ObjectAttributes
        {
            public int Length;
            public IntPtr RootDirectory;
            public IntPtr ObjectName;
            public uint Attributes;
            public IntPtr SecurityDescriptor;
            public IntPtr SecurityQualityOfService;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct IoStatusBlock
        {
            public IntPtr Status;
            public IntPtr Information;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct AttributeTagInfo
        {
            public uint FileAttributes;
            public uint ReparseTag;
        }

        [DllImport("ntdll.dll")]
        private static extern int NtCreateFile(out SafeFileHandle handle, uint access, ref ObjectAttributes attributes, out IoStatusBlock status,
            IntPtr allocationSize, uint fileAttributes, uint share, uint disposition, uint options, IntPtr eaBuffer, uint eaLength);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool GetFileInformationByHandleEx(SafeFileHandle handle, int infoClass, out AttributeTagInfo info, uint size);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool SetFileInformationByHandle(SafeFileHandle handle, int infoClass, ref byte info, uint size);
    }
}
