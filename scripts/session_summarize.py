#!/usr/bin/env python3
"""Shared bounded-summarizer logic.

Used by BOTH the SessionEnd worker hook (session-summarize-worker.sh) and
session_dashboard.py's lazy-summarization pass, so the prompt and the
content-coverage merge logic live in exactly one place instead of being
maintained twice.

Never runs on a per-turn basis — only ever invoked from SessionEnd (once,
per session end) or from a `/recap` run backfilling a stale/missing summary
(capped, on demand). No LLM call happens anywhere else in this system.
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import session_record as sr  # noqa: E402

SECTION_NAMES = ["Status", "Worked On", "Completed", "Stopped At", "Next Action"]
VALID_STATUSES = {"Completed", "In Progress", "Blocked", "Unknown"}

# The `claude -p` subprocess spawned below is ITSELF a full Claude Code
# session with its own session_id and transcript. The CLAUDE_SESSION_LOG_
# SUMMARIZER env var stops that sub-session's own hooks from writing a
# record for it (see session-*-log.sh's recursion guards) — but it does
# NOT stop session_dashboard.py's reconciliation pass from later finding
# that sub-session's transcript and treating it as ordinary unsummarized
# user work, which would recursively spawn more summarizers forever. The
# fix: always run this subprocess from a dedicated scratch directory that
# is not any real project, so its transcript lands under its own isolated
# ~/.claude/projects/<scratch-slug>/ directory — which
# session_dashboard.py explicitly excludes from reconciliation (see
# SUMMARIZER_SCRATCH_SLUG there). This is what actually prevents the
# cascade, independent of the env var guard.
SUMMARIZER_SCRATCH_DIR = Path.home() / ".claude" / "session-logs" / ".summarizer-scratch"

# Every claude -p failure (missing binary, timeout, non-zero exit, empty
# output) used to be swallowed entirely — the only visible symptom was a
# session stuck on "Not yet summarized" forever with no way to tell why.
# This appends one line per failure so that's diagnosable after the fact.
ERROR_LOG = Path.home() / ".claude" / "session-logs" / "summarizer-errors.log"


def _log_failure(label: str, reason: str, stderr_tail: str = "") -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"[{ts}] {label}: {reason}"
    if stderr_tail.strip():
        line += f" — stderr: {stderr_tail.strip()[-500:]!r}"
    try:
        ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
        with ERROR_LOG.open("a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _terminate_process_group(proc: subprocess.Popen, grace_secs: float = 5) -> None:
    """Kill the whole process group, not just `proc` itself.

    `start_new_session=True` at spawn time makes this process its own
    session/group leader (pgid == pid). `proc.terminate()` alone only
    signals that one process — if `claude -p` has spawned its own child
    (a tool subprocess, or a hung one), that child keeps running as an
    orphan, and if anything downstream is reading its stderr via a pipe
    (rather than a plain file), that read blocks forever waiting for the
    orphan to close its inherited fd. Signaling the whole group avoids both
    problems.
    """
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=grace_secs)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _read_and_discard(path: Path) -> str:
    try:
        text = path.read_text(errors="ignore")
    except OSError:
        return ""
    try:
        path.unlink()
    except OSError:
        pass
    return text

PROMPT_TEMPLATE = (
    "Read the Claude Code session transcript at {transcript_path} (JSONL format). "
    "Write a concise session summary as markdown to {body_path} with exactly these "
    "sections: ## Status, ## Worked On, ## Completed, ## Stopped At, ## Next Action. "
    "\n\n"
    "Status must be exactly one of: Completed, In Progress, Blocked, Unknown. "
    "- Completed: the session's stated goal was actually finished, with clear evidence "
    "in the transcript (a file was edited, a command ran and succeeded, a result was "
    "verified) — not merely proposed or talked through. "
    "- In Progress: work was actively continuing and not stuck on anything. "
    "- Blocked: it ended stuck on an error, an explicit question to the user, or "
    "something only the user can resolve. "
    "- Unknown: you cannot tell from the transcript which of the above applies. Use "
    "this rather than guessing — do not default to In Progress or Completed when the "
    "evidence is unclear. "
    "\n\n"
    "In 'Worked On' and 'Completed', classify each concrete claim as one of three "
    "kinds, and say which: "
    "(1) verified — actually implemented/done, with a file edited, a command that ran "
    "and succeeded, or a result you can see confirmed in the transcript; state it as "
    "done, and add a short evidence pointer in parentheses (a file path, the command, "
    "or the test/result) right after the claim; "
    "(2) discussed — only proposed, suggested, or planned, never say 'implemented' or "
    "'completed' for these, say 'discussed' or 'planned' instead; "
    "(3) uncertain — you cannot tell from the transcript whether it actually happened "
    "(e.g. a tool call's result was cut off, ambiguous, or never shown) — say so "
    "explicitly ('unclear whether this succeeded') rather than guessing either way. "
    "\n\n"
    "In 'Next Action': if the requested task is fully done, write exactly 'No required "
    "next action.' Never state an optional idea or suggestion as if it were required — "
    "if there is one, prefix it on its own line with 'Optional:' instead of listing it "
    "as the next step. "
    "\n\n"
    "Base every section only on what actually happened in the transcript, with nothing "
    "invented. If a section has nothing relevant, write 'Nothing notable.' under it. Do "
    "not include a preamble or explanation, just write the file."
)


def parse_summary_body(text: str) -> dict:
    pattern = "|".join(re.escape(n) for n in SECTION_NAMES)
    matches = list(re.finditer(rf"^##\s+({pattern})\s*$", text, re.MULTILINE))
    sections: dict[str, str] = {}
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections[m.group(1)] = text[start:end].strip()

    status = sections.get("Status", "").strip()
    if status not in VALID_STATUSES:
        status = "Unknown"

    return {
        "status": status,
        "worked_on": sections.get("Worked On", "").strip() or "Nothing notable.",
        "completed": sections.get("Completed", "").strip() or "Nothing notable.",
        "stopped_at": sections.get("Stopped At", "").strip() or "Nothing notable.",
        "next_action": sections.get("Next Action", "").strip() or "Nothing notable.",
    }


def run_bounded_summary(
    transcript_path: str,
    project_slug: str,
    session_id: str,
    summary_source: str,
    timeout_secs: int = 180,
) -> bool:
    """Run the bounded, one-shot `claude -p` summarizer against
    `transcript_path` and merge the result into the session's "summary"
    section (gated by content coverage — see session_record.py).

    Returns True if a summary was produced AND accepted by the
    content-coverage guard; False on any failure, timeout, or if a
    since-produced summary already covered at least as much content.
    """
    transcript = Path(transcript_path)
    if not transcript.is_file():
        return False

    bytes_at_read = transcript.stat().st_size
    body_path = transcript.parent / f".tmp-summary-{session_id}-{os.getpid()}.md"
    prompt = PROMPT_TEMPLATE.format(transcript_path=transcript_path, body_path=str(body_path))

    env = dict(os.environ)
    env["CLAUDE_SESSION_LOG_SUMMARIZER"] = "1"

    SUMMARIZER_SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
    stderr_path = transcript.parent / f".tmp-summary-stderr-{session_id}-{os.getpid()}.log"

    try:
        # stderr goes to a plain file, not a pipe: a pipe would make any
        # failure-path read block until every process holding the write end
        # (including a grandchild the subprocess spawns) closes it, which
        # can defeat the timeout below entirely. See _terminate_process_group.
        with stderr_path.open("wb") as stderr_f:
            proc = subprocess.Popen(
                ["claude", "-p", prompt, "--dangerously-skip-permissions", "--allowedTools", "Read,Write"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=stderr_f,
                env=env,
                cwd=str(SUMMARIZER_SCRATCH_DIR),
                start_new_session=True,
            )
    except OSError as e:
        _log_failure(f"session summary ({session_id})", f"failed to launch claude -p: {e}")
        return False

    timed_out = False
    try:
        proc.wait(timeout=timeout_secs)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_process_group(proc)

    if not body_path.exists() or body_path.stat().st_size == 0:
        stderr_text = _read_and_discard(stderr_path)
        reason = f"timed out after {timeout_secs}s" if timed_out else f"exit code {proc.returncode}, no output written"
        _log_failure(f"session summary ({session_id})", reason, stderr_text)
        return False

    _read_and_discard(stderr_path)  # cleanup only; call succeeded, nothing to log

    text = body_path.read_text()
    try:
        body_path.unlink()
    except OSError:
        pass

    parsed = parse_summary_body(text)
    summary_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    data = {
        **parsed,
        "summary_source": summary_source,
        "summary_at": summary_at,
        "summarized_through_bytes": bytes_at_read,
    }
    return sr.merge_section(project_slug, session_id, "summary", data, summary_at)


PROJECT_RECAP_PROMPT_TEMPLATE = (
    "Below are session summaries for the same project, most recent first. "
    "Write AT MOST TWO SHORT SENTENCES to {body_path} synthesizing what these "
    "sessions actually accomplished — do not just concatenate them. "
    "If the sessions cover unrelated threads of work rather than one "
    "continuous effort, say so briefly instead of pretending they are one "
    "task (e.g. 'Two unrelated threads: X and Y.'). Mention the single most "
    "relevant next step only if one clearly stands out across the sessions; "
    "otherwise omit it. Base this only on the summaries given below — do not "
    "invent anything. Do not include a preamble, headings, or quotation "
    "marks, just the plain sentence(s).\n\n"
    "{sessions_text}"
)


def run_project_recap(
    project_slug: str,
    sessions: list[dict],
    timeout_secs: int = 60,
) -> str | None:
    """One bounded `claude -p` call that synthesizes a >=1, <=2 sentence
    project-level recap from already-computed session summaries (no new
    transcript read — this is cheap relative to per-session summarization).

    `sessions` is a list of {"worked_on": ..., "completed": ..., "next_action":
    ...} dicts, most recent first. Returns the recap text, or None on any
    failure/timeout/empty result — callers should treat that as "no recap
    available yet", never fabricate one.
    """
    if not sessions:
        return None

    parts = []
    for i, s in enumerate(sessions, 1):
        parts.append(
            f"Session {i}:\nWorked on: {s.get('worked_on', '')}\n"
            f"Completed: {s.get('completed', '')}\nNext action: {s.get('next_action', '')}"
        )
    sessions_text = "\n\n".join(parts)

    body_path = SUMMARIZER_SCRATCH_DIR / f".tmp-project-recap-{project_slug}-{os.getpid()}.md"
    prompt = PROJECT_RECAP_PROMPT_TEMPLATE.format(body_path=str(body_path), sessions_text=sessions_text)

    env = dict(os.environ)
    env["CLAUDE_SESSION_LOG_SUMMARIZER"] = "1"
    SUMMARIZER_SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
    stderr_path = SUMMARIZER_SCRATCH_DIR / f".tmp-project-recap-stderr-{project_slug}-{os.getpid()}.log"

    try:
        with stderr_path.open("wb") as stderr_f:
            proc = subprocess.Popen(
                ["claude", "-p", prompt, "--dangerously-skip-permissions", "--allowedTools", "Write"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=stderr_f,
                env=env,
                cwd=str(SUMMARIZER_SCRATCH_DIR),
                start_new_session=True,
            )
    except OSError as e:
        _log_failure(f"project recap ({project_slug})", f"failed to launch claude -p: {e}")
        return None

    timed_out = False
    try:
        proc.wait(timeout=timeout_secs)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_process_group(proc)

    if not body_path.exists() or body_path.stat().st_size == 0:
        stderr_text = _read_and_discard(stderr_path)
        reason = f"timed out after {timeout_secs}s" if timed_out else f"exit code {proc.returncode}, no output written"
        _log_failure(f"project recap ({project_slug})", reason, stderr_text)
        return None

    _read_and_discard(stderr_path)

    text = body_path.read_text().strip()
    try:
        body_path.unlink()
    except OSError:
        pass

    return text or None
