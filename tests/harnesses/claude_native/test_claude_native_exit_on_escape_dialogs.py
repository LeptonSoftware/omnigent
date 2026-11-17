"""
The input-box reclaim never presses Escape on a dialog that Escape would quit.

On Claude Code 2.1.299, Escape on the startup "Settings Error" dialog exits
with status 1 and on the folder-trust question with status 0, although both
advertise "Esc to cancel". Claude Code 2.1.326 adds an added-directory trust
question for ``--add-dir`` whose Escape also exits with status 1. A web message
that arrived while one was up used to send that Escape and kill the session's
Claude Code. The panes below are those dialogs as Claude Code draws them, with
a generic path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.harnesses.claude_native import bridge as claude_native_bridge
from omnigent.harnesses.claude_native.bridge import (
    ClaudeTerminalDialog,
    inject_slash_command,
    inject_user_message,
    write_tmux_target,
)

_RULE = "─" * 100
_SETTINGS_ERROR = f"""\
{_RULE}
  Settings Error
  /home/someone/.claude/settings.json
   └ Invalid or malformed JSON
  Files with errors are skipped entirely, not just the invalid settings.
  ❯ 1. Fix with Claude
    2. Exit and fix manually
    3. Continue without these settings
  Enter to confirm · Esc to cancel
"""
_FOLDER_TRUST = f"""\
{_RULE}
 Accessing workspace:
 /home/someone/project
 Quick safety check: Is this a project you created or one you trust? (Like your own code, a
 well-known open source project, or work from your team). If not, take a moment to review what's in
 this folder first.
 Claude Code'll be able to read, edit, and execute files here.
 Security guide
 ❯ No, exit
   Yes, I trust this folder
 Enter to confirm · Esc to cancel
"""
_ADDED_DIRECTORY_TRUST = f"""\
{_RULE}
 Adding a new directory:
 /tmp/omnigent-1000/claude-native/0123456789abcdef0123456789abcdef/agent-skills
 This session hasn't worked here before. Is this a directory you created or one you trust?
 Claude Code'll be able to read, edit, and execute files here. Its skills, commands, and agents
 load and can run commands, and so can the git settings of its repository. Your answer holds for
 this session. Starting Claude Code in this directory and trusting it there lasts, and covers its
 whole repository or everything below it.
 Security guide
 ❯ No, exit
   Yes, add it
 Enter to confirm · Esc to cancel
"""
# The same two trust questions on a short pane, captured from Claude Code 2.1.331
# (paths made generic, wrapping kept): the headline has scrolled off, and the
# first line under no rule is body text or the tail of the path.
_ADDED_DIRECTORY_TRUST_80X12 = """\
 This session hasn't worked here before. Is this a directory you created or one
 you trust?
 Claude Code'll be able to read, edit, and execute files here. Its skills,
 commands, and agents load and can run commands, and so can the git settings of
 its repository. Your answer holds for this session.
 Security guide
 ❯ No, exit
   Yes, add it
 Enter to confirm · Esc to cancel
 Adding: /tmp/omnigent-1000/claude-native/0123456789abcdef0123456789abcdef/agen
t-skills
"""

_ADDED_DIRECTORY_TRUST_50X16 = """\
 ude-native/0123456789abcdef/agent-skills
 This session hasn't worked here before. Is this
 a directory you created or one you trust?
 Claude Code'll be able to read, edit, and
 execute files here. Its skills, commands, and
 agents load and can run commands, and so can the
 git settings of its repository. Your answer
 holds for this session.
 Security guide
 ❯ No, exit
   Yes, add it
 Enter to confirm · Esc to cancel
 Adding: /tmp/omnigent-1000/claude-native/0123456
789abcdef0123456789abcdef/agent-skills
"""

_FOLDER_TRUST_80X12 = """\
 someone/project
 Quick safety check: Is this a project you created or one you trust? (Like your
 own code, a well-known open source project, or work from your team). If not,
 take a moment to review what's in this folder first.
 Claude Code'll be able to read, edit, and execute files here.
 Security guide

 ❯ No, exit
   Yes, I trust this folder

 Enter to confirm · Esc to cancel
"""

# Shorter still (2.1.331 at 80x8, after the person pressed ↓, which the terminal
# key row offers a phone): only the selected option is on screen. At 80x4 the
# added-directory question shows just its footer and the "Adding:" path.
_ADDED_DIRECTORY_TRUST_80X8_AFTER_DOWN = """\
 commands, and agents load and can run commands, and so can the git settings of
 its repository. Your answer holds for this session.
 Security guide
 ❯ Yes, add it
 Enter to confirm · Esc to cancel
 Adding: /tmp/omnigent-1000/claude-native/0123456789abcdef0123456789abcdef/agen
t-skills
"""

_ADDED_DIRECTORY_TRUST_80X4 = """\
 Enter to confirm · Esc to cancel
 Adding: /tmp/omnigent-1000/claude-native/0123456789abcdef0123456789abcdef/agen
t-skills
"""

_FOLDER_TRUST_80X8_AFTER_DOWN = """\
 take a moment to review what's in this folder first.
 Claude Code'll be able to read, edit, and execute files here.
 Security guide

 ❯ Yes, I trust this folder

 Enter to confirm · Esc to cancel
"""

# At 80x8 before any key only "No, exit" is left of the folder question's options.
_FOLDER_TRUST_80X8 = """\
 take a moment to review what's in this folder first.
 Claude Code'll be able to read, edit, and execute files here.
 Security guide

 ❯ No, exit

 Enter to confirm · Esc to cancel
"""

# At 30 columns the footer wraps past "Esc to cancel", so no dismissal hint is
# read, and no option is on screen to name the dialog by.
_ADDED_DIRECTORY_TRUST_30X8 = """\
 Enter to confirm · Esc to
 cancel
 Adding: /tmp/omnigent-1000/cl
aude-native/0123456789abcdef01
23456789abcdef/agent-skills
"""

# At 80x3 the folder question shows only its footer: which dialog it is cannot
# be read, so no Escape is risked.
_FOLDER_TRUST_80X3 = """\

 Enter to confirm · Esc to cancel

"""

# A /resume panel (2.1.331, 100 columns) under command output that printed the
# trust questions' lines: Escape closes the panel, and the lines above its rule
# are conversation, not a dialog.
_RESUME_PANEL_HEAD = f"""\
! printf 'Adding: /srv/skills/review\\nYes, I trust this folder\\n'
  ⎿ \u00a0Adding: /srv/skills/review
     Yes, I trust this folder
  ⎿ \u00a0Interrupted · What should Claude do instead?
❯ /resume
{_RULE}
  Resume session
  ╭{"─" * 94}╮
  │ ⌕ Search…{" " * 84}│
  ╰{"─" * 94}╯
    proj
"""
_RESUME_PANEL_FOOTER = (
    "  Ctrl+A to show all projects · Ctrl+B to only show current branch · Esc to close\n"
)
_RESUME_PANEL_UNDER_MARKER_TEXT = (
    _RESUME_PANEL_HEAD + "  No sessions in this project yet\n" + _RESUME_PANEL_FOOTER
)
# The same panel listing earlier prompts that only contain the markers' words.
_RESUME_PANEL_LISTING_MARKER_WORDS = (
    _RESUME_PANEL_HEAD
    + "  ❯ Yes, I trust this folder, now run the suite\n"
    + "    fix the Adding: /srv path parser\n"
    + _RESUME_PANEL_FOOTER
)

# The trust question's lines echoed into scrollback above a live composer: no
# dialog holds the terminal, so nothing here may count as one.
_TRUST_ECHO_ABOVE_COMPOSER = (
    _ADDED_DIRECTORY_TRUST_80X12 + f"{_RULE}\n❯ what did that question mean?\n{_RULE}\n"
)

# A selection panel Escape closes, with "exit" in an option and its footer:
# the marker is the trust question's own whole option line, not any "exit".
_DISMISSIBLE_PANEL_SAYING_EXIT = f"""\
{_RULE}
 Leave plan mode?
 ❯ 1. Yes, keep going
   2. No, exit plan mode
 Enter to confirm · Esc to exit
"""

_SETTINGS_WARNING = f"""\
{_RULE}
  Settings Warning
  /home/someone/.claude/settings.json
   └ permissions.allow: Invalid permission rule "mcp__*" was skipped
  The values listed above were skipped; the rest of the file is in effect.
  ❯ 1. Continue
    2. Fix with Claude
    3. Exit and fix manually
  Enter to confirm · Esc to cancel
"""


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return self.now


@pytest.fixture
def bridge_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "time", _Clock())
    monkeypatch.setattr(
        claude_native_bridge,
        "_claude_pane_state",
        lambda *_args: claude_native_bridge._ClaudePaneState(alive=True),
    )
    path = tmp_path / "bridge"
    write_tmux_target(path, socket_path=Path("/tmp/example/tmux.sock"), tmux_target="main")
    return path


def _show(monkeypatch: pytest.MonkeyPatch, pane: str) -> list[tuple[str, ...]]:
    sent: list[tuple[str, ...]] = []
    monkeypatch.setattr(claude_native_bridge, "_capture_pane", lambda *_args, **_kw: pane)
    monkeypatch.setattr(claude_native_bridge, "_run_tmux", lambda _sock, *args: sent.append(args))
    return sent


@pytest.mark.parametrize(
    "pane",
    [
        _SETTINGS_ERROR,
        _FOLDER_TRUST,
        _ADDED_DIRECTORY_TRUST,
        _ADDED_DIRECTORY_TRUST_80X12,
        _ADDED_DIRECTORY_TRUST_50X16,
        _FOLDER_TRUST_80X12,
        _ADDED_DIRECTORY_TRUST_80X8_AFTER_DOWN,
        _ADDED_DIRECTORY_TRUST_80X4,
        _FOLDER_TRUST_80X8_AFTER_DOWN,
        _FOLDER_TRUST_80X3,
        _FOLDER_TRUST_80X8,
        _ADDED_DIRECTORY_TRUST_30X8,
    ],
    ids=[
        "settings-error",
        "folder-trust",
        "added-directory-trust",
        "added-directory-trust-80x12",
        "added-directory-trust-50x16",
        "folder-trust-80x12",
        "added-directory-trust-80x8-after-down",
        "added-directory-trust-80x4",
        "folder-trust-80x8-after-down",
        "folder-trust-80x3",
        "folder-trust-80x8",
        "added-directory-trust-30x8",
    ],
)
def test_the_reclaim_leaves_a_dialog_escape_would_quit(
    bridge_dir: Path, monkeypatch: pytest.MonkeyPatch, pane: str
) -> None:
    """No key reaches the pane; the dialog is handed back as one Escape would quit."""
    sent = _show(monkeypatch, pane)

    surface = claude_native_bridge._restore_occupied_input(
        "/tmp/example/tmux.sock", "main", bridge_dir=bridge_dir
    )

    assert surface == claude_native_bridge._ESCAPE_QUITS_SURFACE
    assert sent == [], f"Escape would quit Claude Code here; sent {sent}."


@pytest.mark.parametrize(
    "pane",
    [_SETTINGS_WARNING, _RESUME_PANEL_UNDER_MARKER_TEXT, _RESUME_PANEL_LISTING_MARKER_WORDS],
    ids=["settings-warning", "resume-under-marker-text", "resume-listing-marker-words"],
)
def test_a_panel_escape_closes_is_still_dismissed(
    bridge_dir: Path, monkeypatch: pytest.MonkeyPatch, pane: str
) -> None:
    """Escape closes these (the settings warning continues into the session), so it is sent."""
    sent = _show(monkeypatch, pane)

    claude_native_bridge._restore_occupied_input(
        "/tmp/example/tmux.sock", "main", bridge_dir=bridge_dir
    )

    assert [args[-1] for args in sent] == ["Escape", "Escape"]


@pytest.mark.parametrize(
    "pane",
    [
        _SETTINGS_ERROR,
        _FOLDER_TRUST,
        _ADDED_DIRECTORY_TRUST,
        _ADDED_DIRECTORY_TRUST_80X4,
        _FOLDER_TRUST_80X3,
        _ADDED_DIRECTORY_TRUST_30X8,
    ],
    ids=[
        "settings-error",
        "folder-trust",
        "added-directory-trust",
        "added-directory-trust-80x4",
        "folder-trust-80x3",
        "added-directory-trust-30x8",
    ],
)
def test_a_message_behind_such_a_dialog_is_reported_not_escaped(
    bridge_dir: Path, monkeypatch: pytest.MonkeyPatch, pane: str
) -> None:
    """
    The message is refused at once and Claude Code keeps running.

    On main the reclaim's Escape quit Claude Code. Handed to the readiness gate
    instead, a dialog whose headline has scrolled off is waited on until the
    gate times out, and the executor then reaps the pane.
    """
    sent = _show(monkeypatch, pane)

    with pytest.raises(ClaudeTerminalDialog, match="waiting for an answer in its terminal"):
        inject_user_message(bridge_dir, content="hello from the web")

    assert sent == []
    assert claude_native_bridge.time.monotonic() < 10, "refused only after the readiness gate"


def test_a_settings_command_behind_such_a_dialog_is_refused_without_a_key(
    bridge_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = _show(monkeypatch, _SETTINGS_ERROR)

    # On this fork the command is refused at the reclaim, which hands the dialog back.
    with pytest.raises(ClaudeTerminalDialog, match="waiting for an answer in its terminal"):
        inject_slash_command(bridge_dir, command="/effort high")

    assert sent == []


@pytest.mark.parametrize(
    ("pane", "expected"),
    [
        (_SETTINGS_ERROR, True),
        (_FOLDER_TRUST, True),
        (_ADDED_DIRECTORY_TRUST, True),
        (_ADDED_DIRECTORY_TRUST_80X12, True),
        (_ADDED_DIRECTORY_TRUST_50X16, True),
        (_FOLDER_TRUST_80X12, True),
        (_ADDED_DIRECTORY_TRUST_80X8_AFTER_DOWN, True),
        (_ADDED_DIRECTORY_TRUST_80X4, True),
        (_FOLDER_TRUST_80X8_AFTER_DOWN, True),
        (_FOLDER_TRUST_80X3, True),
        (_FOLDER_TRUST_80X8, True),
        (_ADDED_DIRECTORY_TRUST_30X8, True),
        (_SETTINGS_WARNING, False),
        (_RESUME_PANEL_UNDER_MARKER_TEXT, False),
        (_RESUME_PANEL_LISTING_MARKER_WORDS, False),
        (_TRUST_ECHO_ABOVE_COMPOSER, False),
        (_DISMISSIBLE_PANEL_SAYING_EXIT, False),
        ("", False),
    ],
    ids=[
        "settings-error",
        "folder-trust",
        "added-directory-trust",
        "added-directory-trust-80x12",
        "added-directory-trust-50x16",
        "folder-trust-80x12",
        "added-directory-trust-80x8-after-down",
        "added-directory-trust-80x4",
        "folder-trust-80x8-after-down",
        "folder-trust-80x3",
        "folder-trust-80x8",
        "added-directory-trust-30x8",
        "settings-warning",
        "resume-under-marker-text",
        "resume-listing-marker-words",
        "trust-echo-above-composer",
        "dismissible-panel-saying-exit",
        "empty",
    ],
)
def test_escape_quits_claude(pane: str, expected: bool) -> None:
    assert claude_native_bridge._escape_quits_claude(pane) is expected
