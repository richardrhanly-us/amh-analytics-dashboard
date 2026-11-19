"""Tests for the SortView PreToolUse guard. Run: python -m pytest .claude/hooks/test_guard_production.py

Not collected by the repository's normal `pytest` run (pytest skips dot-directories); run it explicitly.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import guard_production as guard

GUARD = Path(__file__).parent / "guard_production.py"


def shell(command: str, tool: str = "Bash"):
    result = guard.decide({"tool_name": tool, "tool_input": {"command": command}})
    return result[0] if result else None


def edit(path: str, tool: str = "Edit"):
    key = "notebook_path" if tool == "NotebookEdit" else "file_path"
    result = guard.decide({"tool_name": tool, "tool_input": {key: path}})
    return result[0] if result else None


DENY = [
    # commits, pushes and merges, plain and disguised
    "git commit -m x", "git push", "git push origin main", "git merge feature", "git rebase main", "git cherry-pick abc",
    "git revert HEAD", "git -C /c/repo push", 'git -C "C:/My Repo" push', "git -c user.name=x commit -m y",
    "git --git-dir=.git push", "/usr/bin/git push", "git.exe push", "GIT PUSH", "cd x && git push",
    "echo ok; git push", "true || git push", "echo ok | git push", 'bash -c "git push"', "sh -c 'git push origin main'",
    'pwsh -Command "git push"', 'powershell -NoProfile -Command "& git push"', "cmd /c git push", "$(git push)",
    "`git push`", 'g"i"t push', "g''it push", "gi\\t push", 'python -c "import subprocess; subprocess.run([\'git\', \'push\'])"',
    "git config alias.p push", "git -c alias.p=push p", "git commit-tree abc", "git update-ref refs/heads/main abc",
    "git pull", "git pull origin main",
    # GitHub
    "gh pr merge 318", "gh pr merge --squash", "gh release create v1", "gh workflow run deploy",
    "gh api -X PUT repos/o/r/pulls/1/merge", "gh api --method POST repos/o/r/merges", "gh api repos/o/r/x -f a=b",
    "gh secret set X", "gh repo edit --default-branch x",
    # migrations
    "alembic upgrade head", "python -m alembic upgrade head", ".venv/Scripts/alembic.exe downgrade -1",
    ".venv\\Scripts\\alembic stamp head", 'python -c "from alembic import command; command.upgrade(cfg, \'head\')"',
    # AWS, DigitalOcean, Neon
    "aws s3 sync dist/assets s3://b/assets", "aws s3 cp dist/index.html s3://b/index.html --cache-control no-cache",
    "aws cloudfront create-invalidation --distribution-id X --paths /", "aws s3 rm s3://b/x", "aws configure",
    "aws --profile prod s3 sync a s3://b", "doctl apps create-deployment abc", "doctl apps update abc --spec x",
    "neonctl branches restore main", "psql postgresql://u:p@ep-x.us-east-2.aws.neon.tech/db",
    'python -c "import psycopg; psycopg.connect(\'host=ep-1.neon.tech\')"',
    # production HTTP writes
    "curl -X POST https://sortview-app-2p336.ondigitalocean.app/v2/upload",
    "curl -d @x.json https://app.sortviewapp.com/api/x", "curl --data=x https://app.sortviewapp.com/api/x",
    "Invoke-RestMethod -Method Post -Uri https://app.sortviewapp.com/api/x",
    "Invoke-WebRequest https://sortview-app-2p336.ondigitalocean.app/ -Body x",
    # repository scripts that write with --execute
    "python scripts/set_hold_corrected_from.py set --org-slug a --execute",
    "python scripts/set_v2_cutover.py --execute", "python scripts/purge_tenant_data.py --execute",
    # Collector machine changes
    "powershell -File tools/update-collector.ps1", ".\\install.ps1", "pwsh collector/deploy/install-release.ps1",
    "Register-ScheduledTask -TaskName x", "schtasks /create /tn x",
    # uninspectable
    "powershell -EncodedCommand ZQBjAGgAbwA=", "pwsh -enc ZQBjAGgAbwA=", "powershell -e ZQBjAGgAbwA=",
    "iex ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('Z2l0')))",
    # Fix 4 must not open a gap: commands next to, after or around a harmless one are still refused
    "command -v aws; aws s3 cp x s3://b/x", "aws s3 ls && aws s3 sync a s3://b", "aws s3 ls | aws s3 rm s3://b/x",
    "for c in aws; do aws s3 cp a s3://b; done", "aws --region us-east-1 s3 cp a s3://b",
    "aws --profile prod cloudfront create-invalidation --distribution-id X --paths /",
    "aws \\\n  s3 cp a s3://b", "aws `\n  s3 sync a s3://b", "aws ^\r\n s3 cp a s3://b",
    "aws s3 sync dist s3://b `\n  --cache-control no-cache", "aws newservice delete-thing --id 1",
    "aws --output json s3api put-object --bucket b --key k", "aws configure set region us-east-1",
    "command -v doctl || doctl apps create-deployment abc", "doctl apps list; doctl apps delete abc",
    "echo ok\ngit push", "echo ok\r\ngit push", "x & git push", "& git push", "echo ok || git push origin main",
    'python -c "import subprocess; subprocess.run([\'aws\', \'s3\', \'cp\', \'a\', \'s3://b\'])"',
    'bash -c "command -v aws && aws s3 cp a s3://b"',
]

# Only WRITES to the policy files are asked about; reading them is not.
ASK = [
    "sed -i s/a/b/ docs/PROJECT_MANIFEST.md", "echo x >> docs/PROJECT_MANIFEST.md", "echo x > CLAUDE.md",
    "git checkout -- docs/PROJECT_MANIFEST.md", "git restore CLAUDE.md", "Set-Content .claude/settings.json '{}'",
    "cp x .claude/settings.local.json", "Add-Content .\\docs\\PROJECT_MANIFEST.md 'x'",
    "python -c \"open('docs/PROJECT_MANIFEST.md','w').write('')\"", "rm CLAUDE.md",
]

ALLOW = [
    "git status", "git diff", "git log --oneline -5", "git log -S push", "git log --grep merge", "git branch --merged",
    "git show HEAD:docs/merge-policy.md", "git fetch --dry-run", "git ls-remote origin", "git stash list",
    "git worktree list", "git checkout -b push-fix", "git -C /c/repo status", "git rev-parse HEAD",
    "python -m pytest tests -q", "npm ci", "npm run build", "npx vitest run", "npx eslint .", "ruff check .",
    "python scripts/check_release_readiness.py --expect-version 1.0.16",
    "python scripts/set_hold_corrected_from.py review --org-slug a",
    "python scripts/set_hold_corrected_from.py set --org-slug a --sorter-slug b",
    "curl -s https://app.sortviewapp.com/", "curl -sI https://sortview-app-2p336.ondigitalocean.app/",
    "curl -f -s https://app.sortviewapp.com/assets/index-x.js -o app.js",
    "aws --version", "aws s3 ls s3://b", "aws sts get-caller-identity", "aws cloudfront get-distribution --id X",
    "doctl apps list", "doctl apps list-deployments abc", "neonctl projects list",
    "powershell -File collector/deploy/verify-install.ps1", ".\\install.ps1 -VerifyBundleOnly",
    "echo hello", "ls docs", "sha256sum dist/x.js", "Get-FileHash .\\dist\\index.html",
    "psql postgresql://postgres:admin@127.0.0.1:5432/postgres -c 'select 1'",
    "cat C:/Users/me/.claude/projects/x/memory/MEMORY.md",
    # ordinary work on and around the policy files is not interrupted
    "git pull --ff-only", "git p", "cat docs/PROJECT_MANIFEST.md", "type CLAUDE.md", "git diff -- docs/PROJECT_MANIFEST.md",
    "grep -n deploy docs/PROJECT_MANIFEST.md", "Get-Content .\\docs\\PROJECT_MANIFEST.md", "cat .claude/settings.json",
    "python -m pytest .claude/hooks/test_guard_production.py -q", "ruff check .claude/",
    "echo x > .claude/hooks/notes.txt", "sed -i s/a/b/ .claude/hooks/guard_production.py",
    # Fix 4: the false positives found on 2026-11-19
    "command -v aws || echo \"aws -\"", "for c in aws doctl neonctl; do command -v $c || echo \"$c -\"; done",
    "command -v doctl; command -v neonctl", "aws --region us-east-1 s3 ls s3://b", "echo aws; echo doctl",
    "doctl apps list; echo delete", "which aws doctl neonctl",
]


def test_known_false_positive_searching_shell_text_for_a_blocked_phrase():
    """Quoted text is scanned (that is what catches `bash -c "git push"`), so a shell search FOR the phrase is refused
    too. It is a refusal, not a question; Claude's Grep tool is not affected and is the way to search."""
    assert shell('grep -rn "git push" docs/deployment.md') == "deny"
    assert guard.decide({"tool_name": "Grep", "tool_input": {"pattern": "git push"}}) is None


@pytest.mark.parametrize("command", DENY)
def test_denied(command):
    assert shell(command) == "deny", command
    assert shell(command, "PowerShell") == "deny", command


@pytest.mark.parametrize("command", ASK)
def test_asked(command):
    assert shell(command) == "ask", command


@pytest.mark.parametrize("command", ALLOW)
def test_allowed(command):
    assert shell(command) is None, command


@pytest.mark.parametrize("path", [
    "docs/PROJECT_MANIFEST.md", "c:\\Users\\x\\Projects\\amh-analytics-dashboard\\docs\\PROJECT_MANIFEST.md",
    "C:/Users/x/Projects/amh-analytics-dashboard/docs/project_manifest.md", "CLAUDE.md", "c:\\repo\\CLAUDE.md",
    ".claude/settings.json", "c:\\repo\\.claude\\settings.local.json", "C:/Users/x/.claude/settings.json",
])
@pytest.mark.parametrize("tool", ["Edit", "Write", "MultiEdit", "NotebookEdit"])
def test_controlled_edits_are_asked(path, tool):
    assert edit(path, tool) == "ask"


@pytest.mark.parametrize("path", [
    "docs/deployment.md", "src/metrics.py", "frontend/src/reports/HoldsSection.tsx", "docs/PROJECT_MANIFEST.md.bak",
    "C:/Users/x/.claude/projects/p/memory/MEMORY.md", "docs/releases/collector-1.0.16.md",
    "c:/repo/.claude/hooks/guard_production.py", "c:/repo/.claude/hooks/test_guard_production.py",
    "docs/NOTCLAUDE.md",
])
def test_ordinary_edits_are_not_touched(path):
    assert edit(path) is None


def test_other_tools_are_not_touched():
    assert guard.decide({"tool_name": "Read", "tool_input": {"file_path": "docs/PROJECT_MANIFEST.md"}}) is None
    assert guard.decide({"tool_name": "Grep", "tool_input": {"pattern": "git push"}}) is None


def test_unreadable_input_fails_closed():
    assert guard.decide({"tool_name": "Bash", "tool_input": {}})[0] == "deny"
    assert guard.decide({"tool_name": "Write", "tool_input": {}})[0] == "ask"


def _run(stdin: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(GUARD)], input=stdin, capture_output=True, text=True, check=False)


def test_script_output_is_claude_code_hook_json():
    result = _run(json.dumps({"tool_name": "Bash", "tool_input": {"command": "git push"}}))
    assert result.returncode == 0
    out = json.loads(result.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    assert out["permissionDecision"] == "deny"
    assert "SortView guard" in out["permissionDecisionReason"]


def test_script_is_silent_when_it_has_no_opinion():
    result = _run(json.dumps({"tool_name": "Bash", "tool_input": {"command": "git status"}}))
    assert (result.returncode, result.stdout) == (0, "")


def test_script_fails_closed_on_garbage():
    result = _run("not json")
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


# ----- Fixes 1-3: the hook commands as configured in .claude/settings.json ---------------------------------------
# Claude Code runs a hook command through bash (Git Bash on Windows). Exit 2 BLOCKS the tool call; any other non-zero
# exit is a non-blocking error and the call PROCEEDS. So the configured command must turn "Python missing" (exit 127)
# and "guard crashed" (exit 1) into exit 2.

HOOKS_DIR = Path(__file__).parent
SETTINGS = json.loads((HOOKS_DIR.parent / "settings.json").read_text(encoding="utf-8"))
BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(BASH is None, reason="bash is required: Claude Code runs hook commands through it")


def _hook_command(event: str) -> str:
    return SETTINGS["hooks"][event][0]["hooks"][0]["command"]


def _run_hook(event: str, stdin: str, *, path: str, project: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "PATH": path, "CLAUDE_PROJECT_DIR": str(project).replace("\\", "/")}
    return subprocess.run([BASH, "-c", _hook_command(event)], input=stdin, capture_output=True, text=True, env=env,
                          check=False)


def _python_path() -> str:
    return os.pathsep.join([str(Path(sys.executable).parent), str(Path(BASH).parent)])


PUSH = json.dumps({"tool_name": "Bash", "tool_input": {"command": "git push"}})
STATUS = json.dumps({"tool_name": "Bash", "tool_input": {"command": "git status"}})
REPO = HOOKS_DIR.parent.parent


def test_configured_commands_carry_the_fail_closed_fallback():
    assert _hook_command("PreToolUse").rstrip().endswith("exit 2; }")
    assert "|| echo" in _hook_command("SessionStart")
    assert SETTINGS["hooks"]["PreToolUse"][0]["matcher"] == "Bash|PowerShell|Edit|Write|MultiEdit|NotebookEdit"


@needs_bash
@pytest.mark.parametrize("stdin", [PUSH, STATUS])
def test_missing_python_blocks_every_guarded_call(tmp_path, stdin):
    result = _run_hook("PreToolUse", stdin, path=str(tmp_path), project=REPO)  # an empty PATH: no python at all
    assert result.returncode == 2
    assert "SortView guard could not run" in result.stderr


@needs_bash
def test_crashing_guard_blocks(tmp_path):
    (tmp_path / ".claude" / "hooks").mkdir(parents=True)
    (tmp_path / ".claude" / "hooks" / "guard_production.py").write_text("raise RuntimeError('boom')\n")
    result = _run_hook("PreToolUse", PUSH, path=_python_path(), project=tmp_path)
    assert result.returncode == 2
    assert "SortView guard could not run" in result.stderr


@needs_bash
def test_working_guard_decides_normally_through_the_configured_command():
    denied = _run_hook("PreToolUse", PUSH, path=_python_path(), project=REPO)
    assert denied.returncode == 0
    assert json.loads(denied.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
    allowed = _run_hook("PreToolUse", STATUS, path=_python_path(), project=REPO)
    assert (allowed.returncode, allowed.stdout) == (0, "")


@needs_bash
def test_missing_python_session_start_warns_instead_of_staying_silent(tmp_path):
    result = _run_hook("SessionStart", "", path=str(tmp_path), project=REPO)
    assert result.returncode == 0
    assert result.stdout.startswith("WARNING: SortView session facts could not be loaded")


@needs_bash
def test_working_session_start_loads_the_facts():
    result = _run_hook("SessionStart", "", path=_python_path(), project=REPO)
    assert result.returncode == 0
    assert "## Production deployment facts" in result.stdout


# Fix 3: denials that do not depend on Python or on the hooks at all.
REQUIRED_DENIALS = [
    "Bash(git commit:*)", "Bash(git push:*)", "Bash(git merge:*)", "Bash(git rebase:*)", "Bash(gh pr merge:*)",
    "Bash(alembic upgrade:*)", "Bash(alembic downgrade:*)", "Bash(alembic stamp:*)",
    "Bash(aws s3 cp:*)", "Bash(aws s3 sync:*)", "Bash(aws s3 mv:*)", "Bash(aws s3 rm:*)",
    "Bash(aws cloudfront create-invalidation:*)", "Bash(doctl apps create-deployment:*)", "Bash(doctl apps update:*)",
]


@pytest.mark.parametrize("rule", REQUIRED_DENIALS)
def test_independent_permission_denials_are_configured(rule):
    assert rule in SETTINGS["permissions"]["deny"]
