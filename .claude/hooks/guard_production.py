"""Claude Code PreToolUse guard for SortView (CLAUDE.md, "The manifest governs: hard rules, stop and ask").

Reads the hook's JSON on stdin and prints a decision for Claude Code:

  deny  refused outright, no question asked: commits, pushes and merges, migrations, deployments, production writes,
        Collector installs, and commands this guard cannot inspect (encoded PowerShell). The operator runs these.
  ask   the operator approves it in the prompt: edits to the policy files (docs/PROJECT_MANIFEST.md, CLAUDE.md,
        .claude/settings*.json), by an edit tool or by a shell command that writes to them. Reading them is free.
  -     anything else: no output; the normal permission rules apply. Ordinary development is not interrupted.

A BACKSTOP, NOT A SECURITY BOUNDARY. It matches the command text Claude sends. A command assembled at run time (from
variables, a file written earlier, an alias defined outside the command) is not seen, and nothing here applies to what
the operator does outside Claude Code. The rules in CLAUDE.md apply whether or not this guard catches a command.

Fails closed: if the input cannot be read, a shell command is denied and an edit is asked about.
Tests: python -m pytest .claude/hooks/test_guard_production.py
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterable

SHELL_TOOLS = frozenset({"Bash", "PowerShell"})
EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})

POLICY = "CLAUDE.md, 'The manifest governs: hard rules, stop and ask'"

# Characters that separate or quote words in bash, PowerShell, cmd and inline Python. Replaced by spaces, so a command
# wrapped in `bash -c "..."`, `pwsh -Command '...'` or `subprocess.run(["git", "push"])` is seen word by word.
_SEPARATORS = re.compile(r"[\"'`(){}\[\],<>$!\t]")
_QUOTES = re.compile(r"[\"'`]")
# Command boundaries (; | & || && and newlines) become the word ";", so a rule reads only the words of its own command:
# `command -v aws || echo x` is not an aws command. A line continuation (bash `\`, PowerShell backtick, cmd `^` at the
# end of a line) is joined first, so `aws \<newline> s3 cp` is still one command.
_CONTINUATION = re.compile(r"[\\`^][ \t]*\r?\n")
_BOUNDARY = re.compile(r"[;|&\r\n]+")
END = ";"

# git subcommands that create, rewrite or publish commits (CLAUDE.md: Claude never commits or pushes).
_GIT_DENY = frozenset({"commit", "push", "merge", "rebase", "cherry-pick", "revert", "am", "commit-tree", "update-ref",
                       "filter-branch", "filter-repo"})
# git global options that take a separate value word.
_GIT_OPTS_WITH_VALUE = frozenset({"-c", "-C", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env"})
# Known git commands: the first of these after `git` is its subcommand, so a quoted path with a space
# (`git -C "C:/My Repo" push`) cannot hide it.
_GIT_COMMANDS = _GIT_DENY | frozenset({
    "add", "apply", "archive", "bisect", "blame", "branch", "bundle", "cat-file", "check-ignore", "checkout", "cherry",
    "clean", "clone", "config", "count-objects", "credential", "describe", "diff", "diff-tree", "fetch",
    "for-each-ref", "format-patch", "fsck", "gc", "grep", "hash-object", "help", "init", "lfs", "log", "ls-files",
    "ls-remote", "ls-tree", "maintenance", "merge-base", "mktree", "mv", "name-rev", "notes", "prune", "pull",
    "range-diff", "read-tree", "reflog", "remote", "repack", "replace", "reset", "restore", "rev-list", "rev-parse",
    "rm", "shortlog", "show", "show-ref", "sparse-checkout", "stash", "status", "submodule", "switch", "symbolic-ref",
    "tag", "update-index", "var", "verify-commit", "version", "whatchanged", "worktree", "write-tree",
})

# aws: global options that take a value, the services whose every non-read operation is refused, and operation names
# that look like writes in any service.
_AWS_OPTS_WITH_VALUE = frozenset({"--profile", "--region", "--output", "--endpoint-url", "--query", "--color",
                                  "--ca-bundle", "--cli-read-timeout", "--cli-connect-timeout", "--cli-binary-format"})
_AWS_SERVICES = frozenset({
    "s3", "s3api", "s3control", "cloudfront", "iam", "sts", "ec2", "lambda", "rds", "route53", "acm", "secretsmanager",
    "ssm", "ecr", "ecs", "eks", "dynamodb", "sns", "sqs", "ses", "sesv2", "cloudformation", "logs", "cloudwatch", "kms",
    "configure", "organizations", "apigateway", "apigatewayv2", "elbv2", "autoscaling", "wafv2", "amplify", "backup",
    "efs", "events", "sso", "deploy", "codebuild", "codepipeline", "elasticbeanstalk", "lightsail"})
_AWS_READ_OPS = ("ls", "help", "presign", "wait")
_AWS_READ_PREFIXES = ("get-", "list-", "describe-", "head-")
_AWS_WRITE_PREFIXES = ("put", "create", "delete", "update", "remove", "cp", "sync", "mv", "rm", "rb", "mb", "restore",
                       "modify", "attach", "detach", "terminate", "run", "start", "stop", "reboot", "invoke", "publish",
                       "send", "tag", "untag", "set", "associate", "disassociate", "import", "register",
                       "deregister", "enable", "disable", "reset", "cancel", "copy", "upload", "add", "replace",
                       "apply", "deploy", "promote", "revoke", "authorize")

# Verbs that change something, for CLIs whose read commands stay allowed (doctl, neonctl).
_MUTATING_VERBS = frozenset({"create", "create-deployment", "update", "delete", "restart", "rollback", "apply", "deploy",
                             "set", "upsert", "reset", "restore", "propose", "promote", "remove", "rm", "put"})

# Production hosts. Neon is never contacted by Claude at all (CLAUDE.md city rule; no database from a city machine).
_NEVER_HOSTS = ("neon.tech",)
_PRODUCTION_HOSTS = ("sortviewapp.com", "ondigitalocean.app", "api.digitalocean.com", "amazonaws.com", "cloudfront.net")
# An HTTP request that writes: a non-GET method or a request body. curl's flags are case-sensitive (-F is a form
# upload, -f only "fail on error"); PowerShell's are not.
_HTTP_WRITE = re.compile(
    r"(?i:(?:^|\s)(?:-X|--request|-Method|-CustomMethod)\s+(?:POST|PUT|PATCH|DELETE)\b)"
    r"|(?:^|\s)(?:-d|--data(?:-\w+)?|--json|-F|--form|-T|--upload-file)(?:\s|=|$)"
    r"|(?i:(?:^|\s)(?:-Body|-InFile)(?:\s|$))"
)

# Repository scripts that write to a database only with --execute (dry run otherwise).
_EXECUTE_SCRIPTS = ("set_hold_corrected_from", "set_v2_cutover", "set_sorter_bin_layout", "issue_ingest_key",
                    "create_agent_token", "purge_tenant_data")

# Collector install/update/repair scripts: they change a library machine.
_COLLECTOR_SCRIPTS = frozenset({
    "install-collector.ps1", "install-release.ps1", "update-collector.ps1", "update-release.ps1", "setup-collector.ps1",
    "finish-collector-install.ps1", "register-collector-task.ps1", "uninstall-collector.ps1", "restore-backup.ps1",
    "repair-collector-permissions.ps1", "set-collector-api-token.ps1", "configure_v2.ps1", "prepare_v2_pilot.ps1",
    "run-preflight-as-system.ps1", "setup.ps1", "install.ps1"})
_READ_ONLY_INSTALL_FLAGS = ("-verifybundleonly", "-whatif")

# Policy files: their edits need the operator's approval (CLAUDE.md: the manifest is a controlled document).
_POLICY_FILE = re.compile(r"(?:^|/)(?:docs/project_manifest\.md|claude\.md|\.claude/settings(?:\.local)?\.json)$")
_POLICY_NAME_IN_SHELL = re.compile(r"project_manifest\.md|(?<![\w.])claude\.md|\.claude[/\\]settings")
# Shell operations that write to a file they name.
_SHELL_WRITE = re.compile(
    r">|\bsed\s+(?:-\w*\s+)*-i|\b(?:set-content|add-content|out-file|clear-content|remove-item|move-item|copy-item|"
    r"rename-item|new-item|tee|tee-object|truncate|cp|mv|rm|del|erase|copy|move|ren|touch|install|patch)\b|"
    r"\bgit\s+(?:checkout|restore|rm|mv|apply|stash)\b|write_text|write_bytes|open\(.*['\"][wax]"
)


def _variants(command: str) -> list[str]:
    """The command as words, three ways: separators spaced out; quotes dropped (so `g"i"t` is `git`); and quotes and
    backslash escapes dropped (bash `g\\it`). Backslash path separators become `/` in the first two. Command
    boundaries become the word `;` (END)."""
    command = _BOUNDARY.sub(f" {END} ", _CONTINUATION.sub(" ", command))
    spaced = _SEPARATORS.sub(" ", command.replace("\\", "/"))
    joined = _SEPARATORS.sub(" ", _QUOTES.sub("", command.replace("\\", "/")))
    unescaped = _SEPARATORS.sub(" ", _QUOTES.sub("", command.replace("\\", "")))
    return [" ".join(v.split()) for v in (spaced, joined, unescaped)]


def _is_program(word: str, name: str) -> bool:
    """`git`, `/usr/bin/git`, `git.exe`, `C:/Program Files/Git/cmd/git.exe` all name git."""
    base = word.lower().rsplit("/", 1)[-1]
    return base in (name, f"{name}.exe", f"{name}.cmd", f"{name}.bat")


def _git_subcommands(words: list[str]) -> Iterable[tuple[str, list[str]]]:
    """Each git invocation's subcommand (the first known git command after its options) and the words after it."""
    for i, word in enumerate(words):
        if not _is_program(word, "git"):
            continue
        j = i + 1
        while j < len(words) and words[j] != END:
            current = words[j]
            if current.startswith("-"):
                j += 2 if current in _GIT_OPTS_WITH_VALUE else 1
                continue
            if current.lower() in _GIT_COMMANDS:
                yield current.lower(), words[j + 1:]
                break
            j += 1


def _after(words: list[str], name: str) -> Iterable[list[str]]:
    """The words after each occurrence of program `name`, lower-cased, up to the end of that command (END)."""
    for i, word in enumerate(words):
        if _is_program(word, name):
            rest = []
            for w in words[i + 1:]:
                if w == END:
                    break
                rest.append(w.lower())
            yield rest


def _aws_decision(rest: list[str]) -> tuple[str, str] | None:
    """Service and operation after the global options; refused unless clearly a read."""
    ops, i = [], 0
    while i < len(rest) and len(ops) < 2:
        if rest[i].startswith("-"):
            i += 2 if rest[i] in _AWS_OPTS_WITH_VALUE else 1
            continue
        ops.append(rest[i])
        i += 1
    if not ops:
        return None
    service, op = ops[0], (ops[1] if len(ops) > 1 else "")
    read = op in _AWS_READ_OPS or op.startswith(_AWS_READ_PREFIXES)
    if service == "configure" or (service in _AWS_SERVICES and op and not read) or (
            op.startswith(_AWS_WRITE_PREFIXES) and not read):
        return "deny", f"`aws {' '.join(ops)}` changes AWS (S3, CloudFront, credentials) and is the operator's to run"
    return None


def _shell_decision(command: str) -> tuple[str, str] | None:
    for text in _variants(command):
        found = _check_text(text)
        if found is not None:
            return found
    lowered = command.lower().replace("\\", "/")
    if _POLICY_NAME_IN_SHELL.search(lowered) and _SHELL_WRITE.search(lowered):
        return "ask", ("this command appears to write to a policy file (the manifest, CLAUDE.md or .claude/settings); "
                       "that needs the operator's approval")
    return None


def _check_text(text: str) -> tuple[str, str] | None:
    words = text.split(" ")
    lower = text.lower()
    lower_words = lower.split(" ")

    # Uninspectable PowerShell: an encoded command, or a decoded string run through Invoke-Expression.
    if any(_is_program(w, p) for w in lower_words for p in ("powershell", "pwsh")):
        for w in lower_words:
            if w.startswith("-e") and len(w) >= 2 and "encodedcommand".startswith(w[1:]):
                return "deny", "an encoded PowerShell command cannot be inspected"
    if "frombase64string" in lower and ("invoke-expression" in lower_words or "iex" in lower_words):
        return "deny", "a decoded command run through Invoke-Expression cannot be inspected"

    if re.search(r"(?:^|\s)-c\s+alias\.", text):
        return "deny", "an inline git alias could hide a commit or push from this guard"
    for sub, rest in _git_subcommands(words):
        if sub in _GIT_DENY:
            return "deny", f"`git {sub}` creates or publishes commits; Claude never commits, pushes or merges (CLAUDE.md)"
        if sub == "pull" and "--ff-only" not in rest:
            return "deny", "`git pull` can create a merge commit; use `git pull --ff-only`, or the operator pulls"
        if sub == "config" and any("alias" in w.lower() for w in rest):
            return "deny", "defining a git alias could hide a commit or push from this guard"

    for rest in _after(words, "gh"):
        head = " ".join(rest[:2])
        if head in ("pr merge", "workflow run") or (rest[:1] == ["release"] and rest[1:2] not in (["view"], ["list"])):
            return "deny", f"`gh {head}` changes GitHub (merging to main deploys the backend)"
        if rest[:1] in (["secret"], ["variable"], ["ruleset"]) or head in ("repo edit", "repo delete"):
            return "deny", f"`gh {head}` changes repository configuration"
        if rest[:1] == ["api"] and (re.search(r"(?:^|\s)(?:-x|--method)\s+(?:post|put|patch|delete)\b", " ".join(rest))
                                    or any(w in ("-f", "--field", "--raw-field", "--input") for w in rest)):
            return "deny", "`gh api` with a write method changes GitHub"

    if re.search(r"(?:^|[\s/])alembic(?:\.exe)?\s+(?:upgrade|downgrade|stamp)\b", lower) or re.search(
            r"\bcommand\s*\.\s*(?:upgrade|downgrade|stamp)\b", lower):
        return "deny", ("Alembic migrations are run by the operator on the designated non-city machine, never by Claude "
                        "(CLAUDE.md)")

    for rest in _after(words, "aws"):
        found = _aws_decision(rest)
        if found is not None:
            return found
    for name in ("doctl", "neonctl", "neon"):
        for rest in _after(words, name):
            if any(w in _MUTATING_VERBS for w in rest):
                return "deny", f"`{name}` with a changing verb modifies production infrastructure"

    if any(host in lower for host in _NEVER_HOSTS):
        return "deny", "Claude never contacts Neon (no database access from Claude; city rule in CLAUDE.md)"
    if any(host in lower for host in _PRODUCTION_HOSTS) and _HTTP_WRITE.search(text):
        return "deny", "an HTTP write to a production host is a production change"

    if "--execute" in lower_words and any(script in lower for script in _EXECUTE_SCRIPTS):
        return "deny", "this script writes to a database with --execute; the operator runs it"
    if any(w.rsplit("/", 1)[-1] in _COLLECTOR_SCRIPTS for w in lower_words) and not any(
            f in lower_words for f in _READ_ONLY_INSTALL_FLAGS):
        return "deny", "Collector install/update/repair scripts change a library machine; the operator runs them"
    if any(w in ("register-scheduledtask", "unregister-scheduledtask", "set-scheduledtask") for w in lower_words) or \
            re.search(r"\bschtasks(?:\.exe)?\s+/(?:create|delete|change|run)\b", lower):
        return "deny", "changing a Windows scheduled task is the operator's to do"
    return None


def _edit_decision(path: str) -> tuple[str, str] | None:
    normal = path.replace("\\", "/").lower()
    if _POLICY_FILE.search(normal):
        return "ask", f"{path} is a policy file ({POLICY}): edit it only with the operator's explicit approval"
    return None


def decide(event: dict) -> tuple[str, str] | None:
    """The guard's decision for one PreToolUse event, or None for no opinion."""
    tool = event.get("tool_name", "")
    tool_input = event.get("tool_input") or {}
    if tool in SHELL_TOOLS:
        command = tool_input.get("command")
        if not isinstance(command, str):
            return "deny", "the guard could not read this command (fails closed)"
        return _shell_decision(command)
    if tool in EDIT_TOOLS:
        path = tool_input.get("file_path") or tool_input.get("notebook_path")
        if not isinstance(path, str):
            return "ask", "the guard could not read the target path (fails closed)"
        return _edit_decision(path)
    return None


def main() -> int:
    try:
        event = json.loads(sys.stdin.read() or "{}")
        decision = decide(event if isinstance(event, dict) else {})
    except Exception:
        decision = ("deny", "the guard failed to read its input (fails closed)")
    if decision is None:
        return 0
    kind, reason = decision
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": kind,
        "permissionDecisionReason": f"SortView guard: {reason}. See {POLICY}.",
    }}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
