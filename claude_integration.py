"""Claude Code subprocess wrapper with session persistence.

Adapted from polymarket-algo's proven _run_claude() pattern.
Two modes: plan (read-only analysis) and edit (file modifications).
"""

import json
import logging
import os
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
SESSION_FILE = os.path.join(PROJECT_DIR, "logs", "claude_session.txt")
LAST_SESSION_FILE = os.path.join(PROJECT_DIR, ".claude", "last_session.md")

_session_id: str | None = None


def _load_session() -> str | None:
    global _session_id
    if _session_id:
        return _session_id
    try:
        sid = Path(SESSION_FILE).read_text().strip()
        if sid:
            _session_id = sid
            return sid
    except FileNotFoundError:
        pass
    return None


def _save_session(sid: str):
    global _session_id
    _session_id = sid
    Path(SESSION_FILE).parent.mkdir(parents=True, exist_ok=True)
    Path(SESSION_FILE).write_text(sid)


def run_claude(prompt: str, allow_edits: bool = False, timeout: int = 240) -> str:
    """Run claude CLI and return response text.

    Args:
        prompt: The prompt to send to Claude.
        allow_edits: If True, use acceptEdits mode (can modify files).
                     If False, use plan mode (read-only).
        timeout: Subprocess timeout in seconds.

    Returns:
        Claude's response text.
    """
    base_cmd = ["claude", "--print", "--output-format", "json", "--model", "opus"]

    if allow_edits:
        base_cmd.extend(["--permission-mode", "acceptEdits"])
        base_cmd.extend(["--allowedTools", "Read", "Glob", "Grep", "Edit", "Write", "Bash", "Agent"])
        base_cmd.extend(["--max-turns", "15"])
    else:
        base_cmd.extend(["--permission-mode", "plan"])
        base_cmd.extend(["--allowedTools", "Read", "Glob", "Grep", "Bash", "WebFetch", "WebSearch", "Agent"])
        base_cmd.extend(["--max-turns", "10"])

    env = {**os.environ}
    env.pop("CLAUDECODE", None)  # Allow nested claude invocation

    def _run(cmd):
        return subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout, cwd=PROJECT_DIR, env=env,
        )

    def _parse(result):
        try:
            data = json.loads(result.stdout or "{}")
            return data.get("result", ""), data.get("session_id", "")
        except json.JSONDecodeError:
            return (result.stdout or "").strip(), ""

    # Try with existing session for context continuity
    session_id = _load_session()
    resumed = False

    if session_id:
        result = _run(base_cmd + ["--resume", session_id, "-p", prompt])
        response_text, returned_session = _parse(result)

        if result.returncode != 0 and not response_text:
            # Session expired or unavailable — fall back to fresh
            log.warning("Session %s unavailable, starting fresh", session_id)
            result = _run(base_cmd + ["-p", prompt])
            response_text, returned_session = _parse(result)
        else:
            resumed = True
    else:
        result = _run(base_cmd + ["-p", prompt])
        response_text, returned_session = _parse(result)

    # Persist session ID
    if returned_session:
        _save_session(returned_session)

    if result.returncode != 0 and not response_text:
        error_msg = (result.stderr or "unknown").strip()[:500]
        response_text = f"Error: {error_msg}"

    return response_text


def flush_session():
    """Ask Claude to summarize context to last_session.md, then start fresh."""
    session_id = _load_session()
    if not session_id:
        return

    summary_prompt = (
        f"Summarize the key context, decisions, and state from this conversation "
        f"into {LAST_SESSION_FILE}. Include: what was done, what's pending, "
        f"and any important context for the next session."
    )
    try:
        run_claude(summary_prompt, allow_edits=True, timeout=120)
    except Exception:
        log.exception("Failed to flush session")

    # Clear session to start fresh next time
    global _session_id
    _session_id = None
    try:
        Path(SESSION_FILE).unlink()
    except FileNotFoundError:
        pass
