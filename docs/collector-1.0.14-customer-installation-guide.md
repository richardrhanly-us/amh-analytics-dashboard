# Installing SortView Collector 1.0.14

For the library employee or IT technician who installs SortView on the library's Tech Logic computer for the first
time, with someone from SortView available to help.

This guide replaces the *SortView Collector 1.0.2 Customer Installation Guide*. Do not use the 1.0.2 guide: SortView
Setup now does everything that guide did by hand.

**SortView support:** sortviewsuper@gmail.com. Wherever this guide says to contact SortView, write to this address
(or speak to the SortView person helping you with the installation).

---

## 1. What SortView Collector does

SortView Collector is a small program that runs on the Windows computer that receives your Tech Logic sorter's log
files. Every 15 minutes it reads the newest sorting activity from those files and sends it to SortView over a secure
(HTTPS) connection. Your library's staff then see that activity in the SortView app.

- It only reads the Tech Logic files. It does not change them, and it does not change how the sorter works.
- It sends only activity recorded **after** it is installed. Older activity in the files is not sent.
- It runs in the background as a Windows Scheduled Task. Nobody needs to be signed in to the computer, but the
  computer must be switched on.

You install it with **SortView Setup**, a guided installer. You do not type any settings, keys or commands into it.

## 2. What you need

**The computer**

| Requirement | Detail |
|---|---|
| Windows | 64-bit Windows 10 version 1809 or later. SortView has been tested on Windows 10 Enterprise LTSC 2019. Windows Server has not been tested. |
| .NET Framework | 4.7.2 or later (normally already installed by Windows Update). |
| Windows PowerShell | 5.1 (part of Windows 10). |
| Free disk space | At least 1 GB; 5 GB or more is recommended. |
| Network | The computer can reach the internet over HTTPS. |
| Tech Logic files | The computer receives the Tech Logic Checkins, Rejects and ACS log files. |

**People and permissions**

- **A Windows administrator account** on that computer. SortView Setup asks Windows for administrator rights when it
  starts. If you are not an administrator, ask your IT staff to run it or to approve the prompt.
- **Your SortView contact, available during the installation.** They give you the enrollment code (section 4) at the
  moment you need it.

**From SortView**

- The SortView Setup package: a ZIP file named `SortView-Setup-1.0.14.zip`.
- The package's **SHA-256 fingerprint**, a 64-character code that proves the file is the one SortView approved. SortView
  sends it separately from the package.
- A one-time **enrollment code**, given to you during the installation.

If an older SortView program (the SortView agent used before the Collector) is still scheduled on this computer,
SortView Setup tells you so and stops. Contact SortView before installing; they will help move the computer to the
Collector.

## 3. Get the package and check it

1. Save `SortView-Setup-1.0.14.zip` from SortView to the Tech Logic computer, for example to your Downloads folder.
2. Check its fingerprint before you open it. Open **Windows PowerShell**, then type this, with the real location of the
   file:

   ```
   Get-FileHash "C:\Users\<you>\Downloads\SortView-Setup-1.0.14.zip" -Algorithm SHA256
   ```

   This only reads the file; it changes nothing. Compare the long code in the **Hash** column with the fingerprint
   SortView sent you. They must match exactly (capital or small letters do not matter).

   **If they do not match, stop.** Do not open the file. Delete it and contact SortView.
3. Right-click the ZIP file and choose **Extract All**. Keep everything that is extracted together. SortView Setup
   needs the `bundle` folder that sits beside it, so do not run it from inside the ZIP and do not move it out of its
   folder.

**About the "Test build" notice and Windows warnings.** This version of SortView Setup is a package for a
**supervised pilot**, installed with SortView's help. It is **not** a publicly released, digitally signed version of
SortView Setup, and its first pages say so: they call it a *test build* that is *not a released version of SortView
Setup*. That is expected for this package. Windows might also show a warning when you start an unsigned program like
this one; whether it does depends on how your computer and network are set up. Continue only with a package whose
fingerprint matched, and follow your IT department's rules for unsigned software.

## 4. The enrollment code

The enrollment code connects this computer to **your** library in SortView.

- It works **once**.
- It **expires 30 minutes** after SortView creates it.
- SortView creates it while you are installing, shortly before SortView Setup asks for it.

Treat it like a password: do not write it in email, chat, tickets or screenshots. Type or paste it only into SortView
Setup. If it expires before you use it, ask your SortView contact for a new one.

## 5. Install SortView Collector

1. In the extracted folder, open **SortView.Setup.exe**. Approve the Windows administrator prompt.
2. **Welcome.** Read the page and select **Next**.
3. **Check this computer.** SortView Setup checks Windows, .NET Framework, PowerShell, administrator rights, disk space
   and a few other things. It only looks; it changes nothing. A check that fails says how to fix it; fix it and select
   **Check again**. When the checks pass, select **Next**.
4. **Ready to install.** This page lists what setup will do. Select **Next**.
5. **Prepare the installation files.** Setup checks that every file in the package is the one SortView approved, then
   copies the package to a protected folder. Nothing is installed yet. When it finishes, select **Next**.
6. **Install.** Select **Install**. SortView Setup now works through seven steps, shown on the left of the window.
   It stops and asks you when it needs an answer:
   - **"Use these Tech Logic files?"** Setup shows where it expects the files:
     `C:\TLCFinalDlls\Checkins.txt`, `C:\TLCFinalDlls\Rejects.txt` and `C:\TLCFinalDlls\ACS Log.txt`.
     If these are right, select **Use these files**. If your files are somewhere else, select **Choose other files**,
     then select each file with **Browse…** and **Use this file**. A file can have any name and be in any folder.
     You get three tries for each file. Files on this computer's own drive are the most reliable: the Collector runs
     under a Windows system account that often cannot reach network drives.
   - **"Enter the one-time enrollment code from your SortView administrator."** Tell your SortView contact you are
     ready. Enter the code they give you and select **Enroll**.

   You can cancel at any question **before** the enrollment code is sent. Once it has been sent, setup cannot be
   interrupted and finishes on its own; closing the window waits for it.

### The seven steps

| Step | What happens |
|---|---|
| 1. Check the computer and installation package | Setup checks the package again and looks for an existing SortView installation. |
| 2. Find Tech Logic files | You confirm or choose the three Tech Logic files. |
| 3. Connect to SortView | Setup checks that this computer can reach SortView securely. |
| 4. Enroll this computer | Setup sends the enrollment code and saves this computer's enrollment securely. |
| 5. Install SortView Collector | The Collector is installed in `C:\SortView\Collector`. |
| 6. Verify installation | Setup checks that the Collector works, including as the Windows system account, and sets its starting point at the end of each Tech Logic file. |
| 7. Configure the Scheduled Task | Setup turns on the Collector's Scheduled Task. |

### When it is finished

The **Result** area says **"SortView setup is complete"**: the Collector is installed and its Scheduled Task is enabled,
and it runs every 15 minutes. It also shows the installation number and the release (1.0.14). You can close SortView
Setup.

## 6. After installation: the Scheduled Task

SortView Setup creates a Windows Scheduled Task named **SortView Collector** and turns it on. You do not need to do
anything else.

- It runs every 15 minutes, as the Windows system account, whether or not anyone is signed in.
- Its first run is at its next 15-minute trigger, not immediately.
- Each run sends only new activity from the Tech Logic files, so nothing appears in SortView until the sorter has
  recorded something after the installation.

Leave the task turned on. If you ever need to turn it off, ask SortView first.

## 7. If installation stops

If setup cannot finish, the steps on the left show **Stopped here** at the step where it stopped, and the **Result**
area explains what to do in plain words, under **What to do now**. It is one of three kinds:

| What to do now says | What it means |
|---|---|
| **You can try again** | It is safe to select **Install** again (or **Resume setup** if asked). Usually something needs fixing first, such as making the Tech Logic files available, and the message says what. |
| **Your IT staff or SortView administrator needs to help** | Something on the computer, the network or the SortView account needs attention before trying again, such as the computer's internet access. |
| **Contact SortView support** | Do not select Install again, and do not delete any SortView files. SortView needs to look at it first. |

Below that, **Technical details from setup** shows exactly what setup reported. It is meant for your IT staff and
SortView; you do not need to understand it.

**Copy details.** Select **Copy details** and paste the result into your email to SortView support
(sortviewsuper@gmail.com). It copies a short
summary that SortView needs: which step stopped, what kind of problem it was, and the installation's status. It
contains no enrollment code, password, secret, file path or setup log, so it is safe to send.

Keep SortView Setup open until you have copied the details: the setup log under **Show details** is not saved after
the window closes.

**Some situations to know:**

- **An interrupted setup is resumed, not restarted.** If setup stopped after the computer was enrolled, select
  **Install** again (or open SortView Setup again). It asks **"Resume the unfinished SortView setup?"** Select
  **Resume setup**; you will **not** need a new enrollment code.
- **Do not ask for a new enrollment code after a stop unless SortView tells you to.** If the result of an enrollment is
  not known, SortView may already have enrolled the computer, and a different code would be refused.
- **If the Scheduled Task could not be turned on,** the Collector is installed but will not run yet. Ask your IT staff
  to enable the **SortView Collector** task in Windows Task Scheduler. Selecting Install again does not fix this.

## 8. Signing in to SortView for the first time

SortView makes one person at your library the **first owner** of your library's SortView account. You will be told
which email address was used. SortView does not send a password.

The first owner sets their own password:

1. Go to **https://app.sortviewapp.com**.
2. Select **Forgot password?**.
3. Enter the email address SortView used, and send the request.
4. Open the email from SortView and select its link. The link works **once** and **expires after 30 minutes**; if it
   expires, request a new one the same way.
5. Choose a new password (at least 8 characters) and save it.
6. Sign in at **https://app.sortviewapp.com** with your email address and new password.

If no email arrives within a few minutes, check your junk or spam folder, then contact SortView support
(sortviewsuper@gmail.com).

Once signed in, the owner adds the rest of the library's staff on the **Members** page. Each new person sets their
password the same way, with **Forgot password?**.

## 9. Checking that SortView is receiving data

1. Sign in to **https://app.sortviewapp.com** and open your library and its sorter.
2. Until the first activity arrives, the sorter shows **"Waiting for first data"**. This is normal right after
   installation. It is not an error.
3. After the sorter has recorded some activity and the Collector's next 15-minute run has completed, the sorter shows
   its figures instead.

The Collector runs about every 15 minutes, and it has something to send only when the sorter has recorded activity
(items checked in, rejected or routed) since the installation. A quiet sorter means no figures yet, not a problem.

If you know the sorter has been in use since the installation, and its activity still does not appear after the
Collector's next few scheduled runs, contact SortView support (sortviewsuper@gmail.com). SortView can see from its side
whether the Collector has reported in.

## 10. Keep these safe

- Never share the enrollment code, and never put it in email, tickets, screenshots or documents.
- Never share your SortView password.
- Use only a SortView Setup package whose fingerprint matched the one SortView sent you.
- Do not delete files in `C:\SortView\Collector` or `C:\ProgramData\SortViewCollector`. Ask SortView before removing
  or moving the Collector.
