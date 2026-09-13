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
    "In 'Worked On' and 'Completed', distinguish clearly between what was actually "
    "implemented/done (state it as done) versus what was only discussed, proposed, or "
    "planned (say 'discussed' or 'planned', never 'implemented' or 'completed' for "
    "those). "
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

    try:
        proc = subprocess.Popen(
            ["claude", "-p", prompt, "--dangerously-skip-permissions", "--allowedTools", "Read,Write"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            cwd=str(SUMMARIZER_SCRATCH_DIR),
            start_new_session=True,
        )
    except OSError:
        return False

    try:
        proc.wait(timeout=timeout_secs)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    if not body_path.exists() or body_path.stat().st_size == 0:
        return False

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
