#!/usr/bin/env python3
"""Shared bounded-summarizer logic.

Used by BOTH the SessionEnd worker hook (session-summarize-worker.sh) and
session_dashboard.py's lazy-summarization pass, so deterministic evidence
extraction, the prompt, and content-coverage merge logic live in one place.

Never runs on a per-turn basis — only ever invoked from SessionEnd (once,
per session end) or from a `/recap` run backfilling a stale/missing summary
(capped, on demand). No LLM call happens anywhere else in this system.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import session_record as sr  # noqa: E402

SECTION_NAMES = ["Status", "Worked On", "Completed", "Stopped At", "Next Action"]
VALID_STATUSES = {"Completed", "In Progress", "Blocked", "Unknown"}
PROMPT_VERSION = "session-summary-v3-tagged-bullets"

# The model never receives the raw transcript. A deterministic local pass keeps
# only evidence-bearing events and enforces both per-event and whole-input caps.
MAX_EVIDENCE_CHARS = 40_000
MAX_MESSAGE_CHARS = 4_000
MAX_TOOL_INPUT_CHARS = 1_000
MAX_TOOL_RESULT_CHARS = 2_000
MAX_SUMMARY_FIELD_CHARS = 1_500
EVIDENCE_TRUNCATION_MARKER = "\n\n[... middle events omitted by deterministic input budget ...]\n\n"
DEFAULT_SUMMARY_MAX_BUDGET_USD = "0.10"


@dataclass(frozen=True)
class PreparedEvidence:
    text: str
    evidence_hash: str
    transcript_bytes: int

    @property
    def input_chars(self) -> int:
        return len(self.text)


def _summary_max_budget_usd() -> str:
    raw = os.environ.get("RECAP_SUMMARY_MAX_BUDGET_USD", DEFAULT_SUMMARY_MAX_BUDGET_USD)
    try:
        if float(raw) > 0:
            return raw
    except ValueError:
        pass
    return DEFAULT_SUMMARY_MAX_BUDGET_USD


def _claude_summary_command(prompt: str) -> list[str]:
    return [
        "claude", "-p", prompt,
        "--dangerously-skip-permissions",
        "--allowedTools", "Read,Write",
        "--max-budget-usd", _summary_max_budget_usd(),
    ]

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
    "Read the bounded evidence extracted from a Claude Code session at {evidence_path}. "
    "The file was produced deterministically from the transcript: thinking, attachments, "
    "metadata, and oversized tool output were removed before this call. "
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
    "In 'Worked On' and 'Completed', write one bullet per line, each starting with "
    "'- ', at most 5 bullets per section, one clause per bullet — no paragraphs, no "
    "multi-claim bullets. Each bullet must start with exactly one of these three tags, "
    "and say which applies: "
    "'[Verified]' — actually implemented/done, with a file edited, a command that ran "
    "and succeeded, or a result you can see confirmed in the transcript; state it as "
    "done, and end the bullet with a short evidence pointer in parentheses (a file "
    "path, the command, or the test/result); "
    "'[Discussed]' — only proposed, suggested, or planned, never say 'implemented' or "
    "'completed' for these; "
    "'[Uncertain]' — you cannot tell from the transcript whether it actually happened "
    "(e.g. a tool call's result was cut off, ambiguous, or never shown) — say so "
    "explicitly rather than guessing either way. "
    "Example bullet: '- [Verified] Added the retry helper (src/retry.py)'. "
    "\n\n"
    "In 'Next Action': if the requested task is fully done, write exactly 'No required "
    "next action.' Never state an optional idea or suggestion as if it were required — "
    "if there is one, prefix it on its own line with 'Optional:' instead of listing it "
    "as the next step. "
    "\n\n"
    "Base every section only on the supplied evidence, with nothing "
    "invented. If a section has nothing relevant, write 'Nothing notable.' under it. Do "
    "not include a preamble or explanation, just write the file."
)


def _clip(value: str, limit: int) -> str:
    value = value.strip()
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "…"


def _text_blocks(content: object, block_type: str = "text") -> list[str]:
    if isinstance(content, str):
        return [content] if block_type == "text" else []
    if not isinstance(content, list):
        return []
    values = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != block_type:
            continue
        value = block.get("text") if block_type == "text" else block.get("content")
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, list):
            nested = [
                item if isinstance(item, str) else item.get("text", "")
                for item in value
                if isinstance(item, (str, dict))
            ]
            if any(nested):
                values.append("\n".join(nested))
    return values


def _compact_tool_input(value: object) -> str:
    if not isinstance(value, dict):
        return ""
    # Describe what was attempted without copying Write/Edit bodies, complete
    # prompts, or other potentially enormous arguments.
    useful_keys = (
        "command", "cmd", "file_path", "path", "description", "query",
        "pattern", "glob", "url", "old_path", "new_path",
    )
    compact = {key: value[key] for key in useful_keys if key in value}
    if not compact:
        compact = {"argument_keys": sorted(str(key) for key in value.keys())}
    return _clip(json.dumps(compact, sort_keys=True, ensure_ascii=False), MAX_TOOL_INPUT_CHARS)


def _events_from_record(record: dict) -> list[str]:
    event_type = record.get("type")
    message = record.get("message") or {}
    content = message.get("content")
    events: list[str] = []

    if event_type == "user":
        for value in _text_blocks(content):
            if value.strip():
                events.append("USER\n" + _clip(value, MAX_MESSAGE_CHARS))
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                result = "\n".join(_text_blocks([block], "tool_result"))
                state = "error" if block.get("is_error") else "result"
                events.append(f"TOOL {state.upper()}\n" + _clip(result, MAX_TOOL_RESULT_CHARS))

    elif event_type == "assistant":
        for value in _text_blocks(content):
            if value.strip():
                events.append("ASSISTANT\n" + _clip(value, MAX_MESSAGE_CHARS))
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                name = str(block.get("name") or "unknown")
                events.append(f"TOOL USE {name}\n" + _compact_tool_input(block.get("input")))

    return [event for event in events if event.strip()]


def _bounded_event_text(events: list[str]) -> str:
    full = "\n\n".join(events)
    if len(full) <= MAX_EVIDENCE_CHARS:
        return full

    marker = EVIDENCE_TRUNCATION_MARKER
    available = MAX_EVIDENCE_CHARS - len(marker)
    head_budget = available // 3
    tail_budget = available - head_budget
    head = full[:head_budget].rstrip()
    tail = full[-tail_budget:].lstrip()
    return head + marker + tail


def prepare_summary_evidence(transcript_path: str) -> PreparedEvidence | None:
    """Extract a stable, size-bounded evidence document from transcript JSONL."""
    transcript = Path(transcript_path)
    if not transcript.is_file():
        return None
    try:
        transcript_bytes = transcript.stat().st_size
        events: list[str] = []
        with transcript.open("r", encoding="utf-8", errors="ignore") as source:
            for line in source:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    events.extend(_events_from_record(record))
    except OSError:
        return None

    text = _bounded_event_text(events)
    if not text:
        return None
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return PreparedEvidence(text=text, evidence_hash=digest, transcript_bytes=transcript_bytes)


def summary_matches_evidence(summary: object, prepared: PreparedEvidence) -> bool:
    return bool(
        isinstance(summary, dict)
        and summary.get("prompt_version") == PROMPT_VERSION
        and summary.get("evidence_hash") == prepared.evidence_hash
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
        "worked_on": _clip(sections.get("Worked On", ""), MAX_SUMMARY_FIELD_CHARS) or "Nothing notable.",
        "completed": _clip(sections.get("Completed", ""), MAX_SUMMARY_FIELD_CHARS) or "Nothing notable.",
        "stopped_at": _clip(sections.get("Stopped At", ""), MAX_SUMMARY_FIELD_CHARS) or "Nothing notable.",
        "next_action": _clip(sections.get("Next Action", ""), MAX_SUMMARY_FIELD_CHARS) or "Nothing notable.",
    }


def run_bounded_summary(
    transcript_path: str,
    project_slug: str,
    session_id: str,
    summary_source: str,
    timeout_secs: int = 180,
    prepared: PreparedEvidence | None = None,
) -> bool:
    """Extract bounded evidence, run one `claude -p` summary call when the
    evidence hash is not cached, and merge the result into the record's
    "summary" section (gated by content coverage — see session_record.py).

    Returns True if a summary was produced AND accepted by the
    content-coverage guard; False on any failure, timeout, or if a
    since-produced summary already covered at least as much content.
    """
    transcript = Path(transcript_path)
    prepared = prepared or prepare_summary_evidence(transcript_path)
    if prepared is None:
        return False

    event_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    existing = sr.read_record(project_slug, session_id).get("summary", {})
    if summary_matches_evidence(existing, prepared):
        # The transcript may have grown only through ignored metadata. Advance
        # coverage without paying for identical semantic input a second time.
        cached = {key: value for key, value in existing.items() if key != "_ts"}
        cached["summarized_through_bytes"] = prepared.transcript_bytes
        return sr.merge_section(project_slug, session_id, "summary", cached, event_ts)

    body_path = transcript.parent / f".tmp-summary-{session_id}-{os.getpid()}.md"
    evidence_path = transcript.parent / f".tmp-evidence-{session_id}-{os.getpid()}.txt"
    try:
        evidence_path.write_text(prepared.text)
    except OSError as e:
        _log_failure(f"session summary ({session_id})", f"failed to write bounded evidence: {e}")
        return False
    prompt = PROMPT_TEMPLATE.format(evidence_path=str(evidence_path), body_path=str(body_path))

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
                _claude_summary_command(prompt),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=stderr_f,
                env=env,
                cwd=str(SUMMARIZER_SCRATCH_DIR),
                start_new_session=True,
            )
    except OSError as e:
        _read_and_discard(evidence_path)
        _read_and_discard(stderr_path)
        _log_failure(f"session summary ({session_id})", f"failed to launch claude -p: {e}")
        return False

    timed_out = False
    try:
        proc.wait(timeout=timeout_secs)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_process_group(proc)

    _read_and_discard(evidence_path)

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
        "summarized_through_bytes": prepared.transcript_bytes,
        "prompt_version": PROMPT_VERSION,
        "evidence_hash": prepared.evidence_hash,
        "evidence_chars": prepared.input_chars,
    }
    return sr.merge_section(project_slug, session_id, "summary", data, summary_at)
