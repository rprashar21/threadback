#!/usr/bin/env python3
"""Generate a local HTML dashboard of recent Claude Code sessions.

Combines three sources of truth, keyed by session_id:
  1. Per-session JSON records at ~/.claude/session-logs/<slug>/<id>.json
     (written by the SessionStart/Stop/SessionEnd hooks — see
     session_record.py for the section-ownership/concurrency model).
  2. Legacy timestamp-named ~/.claude/session-logs/<slug>/*.md files from
     before this pipeline existed (still parsed for history; deduped by
     session_id when present).
  3. Real transcripts under ~/.claude/projects/<slug>/*.jsonl — this is
     Claude Code's own ground truth. Any transcript with no record at all
     (a crashed/killed session, or a hook that never fired) is reconciled
     into a synthesized entry instead of silently vanishing.

On every run, this script also backfills a real summary (via the same
bounded, one-shot `claude -p` call used by SessionEnd — see
session_summarize.py) for a small, capped number of sessions whose summary
is missing or stale, skipping anything that still looks actively in use.
Status is NEVER inferred from a checkpoint's mere existence — a session
without a real summarizer-produced status is always shown as "Unknown",
honestly labeled with its last known activity.

Stdlib only except for the two sibling modules above. Prints the dashboard
path on stdout (default action), or use --summarize-session to force one
session's summary immediately, bypassing the per-run cap.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import session_record as sr  # noqa: E402
import session_summarize as ss  # noqa: E402

LOG_ROOT = Path.home() / ".claude" / "session-logs"
PROJECTS_ROOT = Path.home() / ".claude" / "projects"
OUT_FILE = LOG_ROOT / "dashboard.html"
SELF_PATH = Path(__file__).resolve()

# session_summarize.py always runs its `claude -p` subprocess from this
# dedicated scratch directory (never a real project) precisely so its own
# transcript can be excluded here — otherwise a summarizer's own session
# would look like ordinary unsummarized user work on the next /recap run,
# and get summarized again, recursively, forever.
SUMMARIZER_SCRATCH_DIR = LOG_ROOT / ".summarizer-scratch"
SUMMARIZER_SCRATCH_SLUG = sr.slugify_cwd(str(SUMMARIZER_SCRATCH_DIR))

# Optional, user-maintained list of project paths to leave out of the
# dashboard (and out of lazy summarization / project-recap generation)
# entirely — one path or path-prefix per line, `#` comments allowed. Not
# present by default; nothing is excluded until a user creates this file.
EXCLUDE_FILE = LOG_ROOT / "recap-exclude.txt"


def load_excluded_prefixes() -> list[str]:
    if not EXCLUDE_FILE.is_file():
        return []
    prefixes = []
    for line in EXCLUDE_FILE.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        prefixes.append(sr.slugify_cwd(line.rstrip("/")))
    return prefixes


# Compared against project *directory slugs* (not raw paths), since a slug
# is the only project identity collect_json_records/collect_legacy_entries/
# build_transcript_index have on hand without re-reading a cwd — and
# slugify_cwd is a plain non-alnum -> "-" substitution, so a path prefix
# match survives slugification as a slug-string prefix match.
EXCLUDED_PREFIXES = load_excluded_prefixes()


def _is_excluded_slug(slug: str) -> bool:
    return any(slug == p or slug.startswith(p + "-") for p in EXCLUDED_PREFIXES)

LAZY_SUMMARY_CAP = 5          # max sessions summarized per /recap run
LIVE_THRESHOLD_SECONDS = 300  # 5 minutes: "still looks in use, don't touch"
SUMMARY_TIMEOUT_SECS = 60     # per-session bound during a /recap run

# Reconciliation exists to catch a session from the last few days that
# crashed or got killed before any hook could record it — not to retroactively
# surface months of pre-existing transcript history the very first time this
# pipeline runs. Orphan transcripts older than this are left out of the
# dashboard entirely (nothing is deleted; they just predate this system).
ORPHAN_RECONCILE_MAX_AGE_DAYS = 14

SECTION_NAMES = ["Status", "Worked On", "Completed", "Stopped At", "Next Action"]
# Older 4-section logs (pre-dashboard) used this shape; still parsed for
# display, but never get a resume command since they carry no session_id.
LEGACY_SECTION_NAMES = ["Worked On", "Discussed", "Completed", "Planned Next"]

JUNK_SNIPPETS = [
    "queued instruction",
    "read another transcript",
    "read the transcript of another",
    "session ended before",
    "asking to read a different transcript",
    "asking claude to read another transcript",
    "another session transcript",
    "a different session transcript",
    "a different transcript",
    "the transcript of another",
]
JUNK_FILENAME_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.jsonl", re.IGNORECASE
)

# The pre-redesign hook's hardcoded placeholder body. A legacy .md carrying
# this text was never actually summarized — treat it as "no summary", not as
# real history, so the lazy-summarization pass can backfill it for real.
LEGACY_PLACEHOLDER_MARKER = "summary generation pending or failed"

VALID_STATUSES = {"Completed", "In Progress", "Blocked", "Unknown"}

NO_ACTION_RE = re.compile(
    r"^(nothing\s+(notable|further|required)\.?|none\s+required\.?|"
    r"no\s+further\s+action(\s+required)?\.?|no\s+action\s+(needed|required)\.?|"
    r"no\s+required\s+next\s+action\.?)",
    re.IGNORECASE,
)
OPTIONAL_SPLIT_RE = re.compile(r"optional[^:]*:\s*(.+)", re.IGNORECASE | re.DOTALL)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# --- Legacy .md parsing (unchanged parsing rules, kept for history) --------

@dataclass
class LegacyEntry:
    session_id: str | None
    cwd: str | None
    ended_at: str
    status: str
    worked_on: str
    completed: str
    stopped_at: str
    next_action: str
    is_placeholder: bool
    source_file: str


def parse_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    _, fm_block, body = parts
    fm: dict[str, str] = {}
    for line in fm_block.strip().splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            fm[key.strip()] = value.strip()
    return fm, body


def parse_sections(body: str, names: list[str]) -> dict[str, str]:
    sections: dict[str, str] = {}
    pattern = "|".join(re.escape(n) for n in names)
    matches = list(re.finditer(rf"^##\s+({pattern})\s*$", body, re.MULTILINE))
    for i, m in enumerate(matches):
        name = m.group(1)
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        sections[name] = body[start:end].strip()
    return sections


def is_junk(all_sections: dict[str, str]) -> bool:
    combined = " ".join(all_sections.values()).lower()
    if any(snippet in combined for snippet in JUNK_SNIPPETS):
        return True
    if JUNK_FILENAME_RE.search(combined):
        return True
    values = [v.strip().rstrip(".").lower() for v in all_sections.values()]
    if values and all(v in ("", "nothing notable") for v in values):
        return True
    return False


def load_legacy_entry(path: Path) -> LegacyEntry | None:
    try:
        text = path.read_text()
    except OSError:
        return None

    fm, body = parse_frontmatter(text)
    session_id = fm.get("session_id") or None
    cwd = fm.get("cwd") or None
    ended_at = fm.get("ended_at") or ""

    sections = parse_sections(body, SECTION_NAMES)
    if not sections:
        sections = parse_sections(body, LEGACY_SECTION_NAMES)

    worked_on = sections.get("Worked On", "").strip()
    if is_junk(sections):
        return None

    if not ended_at:
        m = re.match(r"(\d{4}-\d{2}-\d{2})_(\d{2})(\d{2})(\d{2})", path.stem)
        if m:
            ended_at = f"{m.group(1)}T{m.group(2)}:{m.group(3)}:{m.group(4)}Z"
        else:
            ended_at = "1970-01-01T00:00:00Z"

    status = sections.get("Status", "").strip()
    if status not in VALID_STATUSES:
        status = "Unknown"

    is_placeholder = LEGACY_PLACEHOLDER_MARKER in worked_on.lower()

    return LegacyEntry(
        session_id=session_id,
        cwd=cwd,
        ended_at=ended_at,
        status=status,
        worked_on=worked_on or "Nothing notable.",
        completed=sections.get("Completed", "").strip() or "Nothing notable.",
        stopped_at=sections.get("Stopped At", "").strip() or "Nothing notable.",
        next_action=sections.get("Next Action", "").strip() or "Nothing notable.",
        is_placeholder=is_placeholder,
        source_file=str(path),
    )


def collect_legacy_entries() -> tuple[dict[str, LegacyEntry], list[LegacyEntry]]:
    """Returns (best entry per session_id, entries with no session_id at all)."""
    by_id: dict[str, LegacyEntry] = {}
    no_id: list[LegacyEntry] = []
    if not LOG_ROOT.is_dir():
        return by_id, no_id
    for project_dir in sorted(LOG_ROOT.iterdir()):
        if not project_dir.is_dir() or project_dir.name == SUMMARIZER_SCRATCH_SLUG:
            continue
        if _is_excluded_slug(project_dir.name):
            continue
        for md_file in sorted(project_dir.glob("*.md")):
            entry = load_legacy_entry(md_file)
            if entry is None:
                continue
            if not entry.session_id:
                no_id.append(entry)
                continue
            existing = by_id.get(entry.session_id)
            if existing is None:
                by_id[entry.session_id] = entry
                continue
            # Prefer a real summary over a stuck placeholder; between two
            # real ones, prefer the more recent.
            if existing.is_placeholder and not entry.is_placeholder:
                by_id[entry.session_id] = entry
            elif existing.is_placeholder == entry.is_placeholder and entry.ended_at > existing.ended_at:
                by_id[entry.session_id] = entry
    return by_id, no_id


# --- JSON record collection -------------------------------------------------

def collect_json_records() -> dict[str, dict]:
    """session_id -> record dict ({"start": ..., "checkpoint": ..., "end": ...,
    "summary": ...}), scanning every ~/.claude/session-logs/<slug>/<id>.json."""
    records: dict[str, dict] = {}
    if not LOG_ROOT.is_dir():
        return records
    for project_dir in sorted(LOG_ROOT.iterdir()):
        if not project_dir.is_dir() or project_dir.name == SUMMARIZER_SCRATCH_SLUG:
            continue
        if _is_excluded_slug(project_dir.name):
            continue
        for json_file in sorted(project_dir.glob("*.json")):
            session_id = json_file.stem
            try:
                records[session_id] = json.loads(json_file.read_text())
            except (OSError, json.JSONDecodeError):
                continue
    return records


# --- Combined per-session model --------------------------------------------

@dataclass
class Combined:
    session_id: str | None
    cwd: str | None
    record: dict = field(default_factory=dict)
    legacy: LegacyEntry | None = None
    transcript_path: Path | None = None
    is_orphan: bool = False


def build_transcript_index() -> dict[str, tuple[str, Path]]:
    """session_id -> (project_slug, transcript_path), scanning every real
    transcript under ~/.claude/projects/ once. This is the one place project
    identity is looked up from — a session's OWN record may not carry a cwd
    (e.g. a summary-only record for a session that predates the
    SessionStart/Stop hooks, or one recovered by reconciliation), but its
    transcript's location on disk is always authoritative."""
    index: dict[str, tuple[str, Path]] = {}
    if not PROJECTS_ROOT.is_dir():
        return index
    for project_dir in sorted(PROJECTS_ROOT.iterdir()):
        if not project_dir.is_dir() or project_dir.name == SUMMARIZER_SCRATCH_SLUG:
            continue
        if _is_excluded_slug(project_dir.name):
            continue
        for jsonl_file in project_dir.glob("*.jsonl"):
            index[jsonl_file.stem] = (project_dir.name, jsonl_file)
    return index


def build_combined(
    legacy_by_id: dict[str, LegacyEntry],
    json_records: dict[str, dict],
    transcript_index: dict[str, tuple[str, Path]],
) -> dict[str, Combined]:
    combined: dict[str, Combined] = {}

    for sid, legacy in legacy_by_id.items():
        combined[sid] = Combined(session_id=sid, cwd=legacy.cwd, legacy=legacy)

    for sid, record in json_records.items():
        cwd = (
            record.get("end", {}).get("cwd")
            or record.get("start", {}).get("cwd")
            or (combined[sid].cwd if sid in combined else None)
        )
        if sid in combined:
            combined[sid].record = record
            combined[sid].cwd = cwd or combined[sid].cwd
        else:
            combined[sid] = Combined(session_id=sid, cwd=cwd, record=record)

    # Resolve each entry's real transcript path directly from the index —
    # authoritative regardless of whether the record itself carries a cwd
    # (a record made only of a "summary" section, e.g. one written by lazy
    # summarization for a session that had no cwd on disk yet, carries no
    # cwd at all — looking it up via slugify_cwd(cwd) would silently fail
    # forever in that case).
    for c in combined.values():
        entry = transcript_index.get(c.session_id) if c.session_id else None
        if entry:
            _, path = entry
            c.transcript_path = path

    # Recover a still-missing cwd from another session in the same project
    # that does have one recorded — keyed by the transcript's real parent
    # directory (ground truth), not a guessed/reversed slug. Without this,
    # a session summarized before it had a known cwd (or one whose only
    # record section is "summary") would permanently drop out of its
    # project's grouping on every subsequent run.
    slug_to_cwd: dict[str, str] = {}
    for c in combined.values():
        if c.cwd and c.transcript_path is not None:
            slug_to_cwd.setdefault(c.transcript_path.parent.name, c.cwd)
    for c in combined.values():
        if not c.cwd and c.transcript_path is not None:
            c.cwd = slug_to_cwd.get(c.transcript_path.parent.name)

    return combined


def read_cwd_from_transcript(path: Path, max_lines: int = 20) -> str | None:
    """Fallback cwd source: every real user-turn line in a transcript
    carries its own `cwd` field, independent of whether any hook ever ran
    for that session. Only scans the first few lines (cwd doesn't change
    within a session) so this stays cheap even for large transcripts."""
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as f:
            for i, line in enumerate(f):
                if i >= max_lines:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cwd = d.get("cwd")
                if cwd:
                    return cwd
    except OSError:
        return None
    return None


def fill_missing_cwd_from_transcripts(combined: dict[str, Combined]) -> None:
    """Last-resort cwd recovery for sessions with no hook-written record and
    no same-project sibling to borrow a cwd from (see the recovery pass in
    build_combined) — e.g. a session whose hooks never fired at all. Without
    this, such a session shows as an unnamed "(unknown project)" duplicate
    with no resume command, even though its own transcript knows its cwd."""
    for c in combined.values():
        if not c.cwd and c.transcript_path is not None:
            c.cwd = read_cwd_from_transcript(c.transcript_path)


def reconcile_orphans(combined: dict[str, Combined], transcript_index: dict[str, tuple[str, Path]]) -> None:
    """Any real transcript with no record/legacy entry at all — e.g. the
    session crashed before any hook could run — gets a synthesized entry
    instead of silently vanishing from the dashboard."""
    slug_to_cwd: dict[str, str] = {}
    for c in combined.values():
        if c.cwd and c.transcript_path is not None:
            slug_to_cwd.setdefault(c.transcript_path.parent.name, c.cwd)

    cutoff = _now() - timedelta(days=ORPHAN_RECONCILE_MAX_AGE_DAYS)

    for sid, (slug, jsonl_file) in transcript_index.items():
        if sid in combined:
            continue
        try:
            mtime = datetime.fromtimestamp(jsonl_file.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
        if mtime < cutoff:
            continue
        combined[sid] = Combined(
            session_id=sid,
            cwd=slug_to_cwd.get(slug),
            transcript_path=jsonl_file,
            is_orphan=True,
        )


# --- Context usage (transcript-derived, no LLM call) ------------------------

def scan_transcript_usage(path: Path) -> dict | None:
    """Best-effort peek at a transcript's own recorded token usage.

    Peak context and total output are DIFFERENT metrics, computed
    separately and never conflated:
      - peak_context_tokens: the highest (input + cache_creation_input +
        cache_read_input) seen on any single assistant turn — a proxy for
        how full the context window got at its fullest point.
      - total_output_tokens: the sum of output_tokens across every turn —
        total generated output for the whole session.
    Returns None (never an estimate) if the transcript has no usage blocks
    at all.
    """
    peak = 0
    total_output = 0
    found = False
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("type") != "assistant":
                    continue
                usage = (d.get("message") or {}).get("usage")
                if not usage:
                    continue
                found = True
                context_tokens = (
                    usage.get("input_tokens", 0)
                    + usage.get("cache_creation_input_tokens", 0)
                    + usage.get("cache_read_input_tokens", 0)
                )
                peak = max(peak, context_tokens)
                total_output += usage.get("output_tokens", 0)
    except OSError:
        return None
    if not found:
        return None
    return {"peak_context_tokens": peak, "total_output_tokens": total_output}


def get_usage_stats(c: Combined) -> dict | None:
    """Cached, transcript-mtime-gated context-usage numbers for one session
    — recomputed only when the transcript has grown since it was last
    scanned (same mtime-guard pattern as "checkpoint"), so a full rescan
    doesn't happen on every single /recap run."""
    if c.transcript_path is None or not c.session_id:
        return None
    try:
        mtime_iso = _iso(datetime.fromtimestamp(c.transcript_path.stat().st_mtime, tz=timezone.utc))
    except OSError:
        return None

    cached = c.record.get("usage")
    if cached and cached.get("_ts", "") >= mtime_iso:
        return cached if cached.get("peak_context_tokens") is not None else None

    stats = scan_transcript_usage(c.transcript_path)
    slug = c.transcript_path.parent.name
    sr.merge_section(
        slug, c.session_id, "usage",
        stats if stats is not None else {"peak_context_tokens": None, "total_output_tokens": None},
        mtime_iso,
    )
    return stats


# --- Staleness / liveness / lazy summarization ------------------------------

def _last_activity(c: Combined) -> datetime | None:
    candidates: list[datetime] = []
    checkpoint = c.record.get("checkpoint", {})
    end = c.record.get("end", {})
    start = c.record.get("start", {})
    for ts in (checkpoint.get("checked_at"), end.get("ended_at"), start.get("started_at")):
        dt = _parse_ts(ts)
        if dt:
            candidates.append(dt)
    if c.legacy:
        dt = _parse_ts(c.legacy.ended_at)
        if dt:
            candidates.append(dt)
    if c.transcript_path is not None:
        try:
            candidates.append(
                datetime.fromtimestamp(c.transcript_path.stat().st_mtime, tz=timezone.utc)
            )
        except OSError:
            pass
    return max(candidates) if candidates else None


def _current_known_bytes(c: Combined) -> int | None:
    checkpoint = c.record.get("checkpoint", {})
    if "transcript_bytes" in checkpoint:
        return checkpoint["transcript_bytes"]
    if c.transcript_path is not None:
        try:
            return c.transcript_path.stat().st_size
        except OSError:
            return None
    return None


def _has_ended(c: Combined) -> bool:
    return bool(c.record.get("end")) or c.legacy is not None


def _needs_summary(c: Combined) -> bool:
    summary = c.record.get("summary")
    has_real_summary = bool(summary) or (c.legacy is not None and not c.legacy.is_placeholder)
    if not has_real_summary:
        return True
    if summary:
        known_bytes = _current_known_bytes(c)
        if known_bytes is not None and known_bytes > summary.get("summarized_through_bytes", -1):
            return True
    return False


def _is_live(c: Combined, now: datetime) -> bool:
    if _has_ended(c):
        return False
    last_active = _last_activity(c)
    if last_active is None:
        return False
    return (now - last_active).total_seconds() < LIVE_THRESHOLD_SECONDS


def run_lazy_summarization(combined: dict[str, Combined], force_session_id: str | None = None) -> None:
    now = _now()

    if force_session_id is not None:
        c = combined.get(force_session_id)
        if c is None:
            print(f"No known session {force_session_id}.", file=sys.stderr)
            return
        if _is_live(c, now):
            print(
                f"Session {force_session_id} looks active right now (activity within the "
                "last 5 minutes) — refusing to summarize a still-changing conversation.",
                file=sys.stderr,
            )
            return
        if c.transcript_path is None:
            print(f"No transcript found on disk for session {force_session_id}.", file=sys.stderr)
            return
        slug = sr.slugify_cwd(c.cwd) if c.cwd else c.transcript_path.parent.name
        ss.run_bounded_summary(
            str(c.transcript_path), slug, force_session_id, "recap_reconciliation",
            timeout_secs=180,
        )
        return

    candidates = [
        c for c in combined.values()
        if _needs_summary(c) and not _is_live(c, now) and c.transcript_path is not None
    ]
    candidates.sort(key=lambda c: _last_activity(c) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    candidates = candidates[:LAZY_SUMMARY_CAP]
    if not candidates:
        return

    def _summarize(c: Combined) -> None:
        slug = sr.slugify_cwd(c.cwd) if c.cwd else c.transcript_path.parent.name
        ss.run_bounded_summary(
            str(c.transcript_path), slug, c.session_id, "recap_reconciliation",
            timeout_secs=SUMMARY_TIMEOUT_SECS,
        )

    # Each candidate is a separate `claude -p` subprocess targeting a
    # different session's record file (session_record.py's per-session flock
    # already makes concurrent writes safe), so there's no reason to
    # serialize them — running sequentially made /recap's worst case
    # ~LAZY_SUMMARY_CAP x SUMMARY_TIMEOUT_SECS instead of ~1x.
    with ThreadPoolExecutor(max_workers=len(candidates)) as pool:
        list(pool.map(_summarize, candidates))


# --- Display-only derived fields --------------------------------------------

def project_display_name(cwd: str | None, fallback_slug: str) -> str:
    if cwd:
        return Path(cwd).name or cwd
    return f"(unknown project — {fallback_slug})"


def build_resume_command(cwd: str | None, session_id: str | None) -> str | None:
    if not cwd or not session_id:
        return None
    return f"cd {shlex.quote(cwd)} && claude --resume {shlex.quote(session_id)}"


def _strip_markdown_inline(text: str) -> str:
    text = re.sub(r"^[-*]\s+", "", text.strip())
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    return text.strip()


def _first_sentence(text: str) -> str:
    text = text.strip()
    if not text:
        return ""
    first_line = text.splitlines()[0].strip()
    first_line = _strip_markdown_inline(first_line)
    m = re.search(r"(.+?[.!?])(\s|$)", first_line)
    sentence = m.group(1) if m else first_line
    return sentence.strip()


def make_summary(worked_on: str, completed: str) -> str:
    for candidate in (worked_on, completed):
        norm = candidate.strip().rstrip(".").lower()
        if norm and norm != "nothing notable":
            sentence = _first_sentence(candidate)
            if sentence:
                return sentence
    return "No summary available."


def make_title(summary: str, worked_on: str, project: str) -> str:
    basis = summary if summary and summary not in ("No summary available.", "Not yet summarized.") else _first_sentence(worked_on)
    basis = basis.rstrip(".!?")
    words = basis.split()
    if not words:
        return f"{project} session"
    title_words = words[:10]
    title = " ".join(title_words)
    if len(words) > 10:
        title += "…"
    return title


def split_next_action(status: str, text: str) -> tuple[str | None, str | None]:
    stripped = text.strip()
    if status != "Completed":
        return (stripped or None), None

    opt_match = OPTIONAL_SPLIT_RE.search(stripped)
    optional = opt_match.group(1).strip() if opt_match else None
    optional = optional or None

    if not stripped or NO_ACTION_RE.match(stripped):
        return None, optional

    if opt_match:
        required = stripped[: opt_match.start()].strip().rstrip(".") or None
        return required, optional

    return stripped, None


def shorten_cwd(cwd: str | None) -> str:
    if not cwd:
        return "(path unknown)"
    home = str(Path.home())
    if cwd == home:
        return "~"
    if cwd.startswith(home + "/"):
        return "~" + cwd[len(home):]
    return cwd


def build_session_data(combined: dict[str, Combined]) -> list[dict]:
    now = _now()
    data = []

    for c in combined.values():
        cwd = c.cwd
        fallback_slug = (
            c.transcript_path.parent.name if c.transcript_path is not None else "unknown"
        )
        project = project_display_name(cwd, fallback_slug)

        summary_section = c.record.get("summary")
        if not summary_section and c.legacy and not c.legacy.is_placeholder:
            summary_section = {
                "status": c.legacy.status,
                "worked_on": c.legacy.worked_on,
                "completed": c.legacy.completed,
                "stopped_at": c.legacy.stopped_at,
                "next_action": c.legacy.next_action,
                "summary_source": "legacy_md",
                "summary_at": c.legacy.ended_at,
            }

        last_active = _last_activity(c)
        last_active_iso = _iso(last_active) if last_active else "1970-01-01T00:00:00Z"

        if summary_section:
            status = summary_section["status"] if summary_section["status"] in VALID_STATUSES else "Unknown"
            worked_on = summary_section["worked_on"]
            completed = summary_section["completed"]
            stopped_at = summary_section["stopped_at"]
            next_action_text = summary_section["next_action"]
            summary_source = summary_section.get("summary_source")
            summary_at = summary_section.get("summary_at")
            summary = make_summary(worked_on, completed)
            title = make_title(summary, worked_on, project)
            pending_label = None
        else:
            status = "Unknown"
            worked_on = "Not yet summarized."
            completed = "Not yet summarized."
            stopped_at = "Not yet summarized."
            next_action_text = ""
            summary_source = None
            summary_at = None
            summary = "Not yet summarized."
            title = f"{project} session"
            if _is_live(c, now):
                pending_label = "Looks active right now — skipped this run."
            else:
                pending_label = "Queued — will summarize on a later /recap run."

        next_required, next_optional = split_next_action(status, next_action_text)

        stopped_norm = stopped_at.strip().rstrip(".").lower()
        if status == "Completed" and stopped_norm in ("", "nothing notable"):
            stopped_at_visible = None
        else:
            stopped_at_visible = stopped_at if summary_section else None

        force_command = None
        if pending_label is not None and c.session_id:
            force_command = f"python3 {SELF_PATH} --summarize-session {c.session_id}"

        usage_stats = get_usage_stats(c)

        data.append(
            {
                "project": project,
                "cwd": cwd,
                "cwd_short": shorten_cwd(cwd),
                "ended_at": last_active_iso,
                "status": status,
                "title": title,
                "summary": summary,
                "worked_on": worked_on,
                "completed": completed,
                "stopped_at_visible": stopped_at_visible,
                "next_action_required": next_required,
                "next_action_optional": next_optional,
                "resume_command": build_resume_command(cwd, c.session_id),
                "summary_source": summary_source,
                "summary_at": summary_at,
                "pending_label": pending_label,
                "force_command": force_command,
                "context_peak_tokens": usage_stats["peak_context_tokens"] if usage_stats else None,
                "context_total_output_tokens": usage_stats["total_output_tokens"] if usage_stats else None,
            }
        )

    # No-session-id legacy entries (very old format) can't be deduped or
    # cross-checked against transcripts; show as before.
    return data


# --- Project-level recap synthesis ------------------------------------------

PROJECT_RECAP_CANDIDATE_COUNT = 5  # how many recent summarized sessions feed one recap
PROJECT_RECAP_CAP = 3              # max project recaps (re)generated per /recap run


def _project_recap_path(slug: str) -> Path:
    return LOG_ROOT / slug / "project_recap.json"


def _read_project_recap(slug: str) -> dict | None:
    try:
        return json.loads(_project_recap_path(slug).read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _write_project_recap(slug: str, data: dict) -> None:
    path = _project_recap_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".project_recap.tmp-{os.getpid()}.json"
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def _effective_slug(c: Combined) -> str | None:
    if c.transcript_path is not None:
        return c.transcript_path.parent.name
    if c.cwd:
        return sr.slugify_cwd(c.cwd)
    return None


def _session_summary_for_recap(c: Combined) -> dict | None:
    """A session's own summary text, in the shape run_project_recap wants —
    only for sessions with a REAL summary (never "Not yet summarized.")."""
    summary = c.record.get("summary")
    if not summary and c.legacy and not c.legacy.is_placeholder:
        summary = {
            "worked_on": c.legacy.worked_on,
            "completed": c.legacy.completed,
            "next_action": c.legacy.next_action,
            "summary_at": c.legacy.ended_at,
        }
    if not summary:
        return None
    return {
        "session_id": c.session_id,
        "worked_on": summary.get("worked_on", ""),
        "completed": summary.get("completed", ""),
        "next_action": summary.get("next_action", ""),
        "summary_at": summary.get("summary_at", ""),
    }


def run_project_recaps(combined: dict[str, Combined]) -> dict[str, str]:
    """Synthesize a short recap per project from its most recent summarized
    sessions, caching the result in <slug>/project_recap.json and only
    regenerating when the contributing session set/summaries have changed
    since the cached recap was made. Capped per run like lazy session
    summarization — this makes one `claude -p` call per (re)generated
    project, not per session.

    Returns {project_display_name: recap_text} for every project that has
    a usable recap (cached or freshly generated) — projects with no real
    session summaries yet are simply absent, never given a fabricated line.
    """
    now = _now()
    by_slug: dict[str, list[Combined]] = {}
    for c in combined.values():
        slug = _effective_slug(c)
        if slug is None or slug == SUMMARIZER_SCRATCH_SLUG:
            continue
        by_slug.setdefault(slug, []).append(c)

    recaps: dict[str, str] = {}
    to_regenerate: list[tuple[str, list[dict], str, list[list[str]]]] = []

    for slug, sessions in by_slug.items():
        sessions.sort(key=lambda c: _last_activity(c) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        summarized = [_session_summary_for_recap(c) for c in sessions]
        summarized = [s for s in summarized if s][:PROJECT_RECAP_CANDIDATE_COUNT]
        if not summarized:
            continue

        rep_cwd = next((c.cwd for c in sessions if c.cwd), None)
        display_name = project_display_name(rep_cwd, slug)

        signature = [[s["session_id"] or "", s["summary_at"]] for s in summarized]
        cached = _read_project_recap(slug)
        if cached and cached.get("based_on") == signature:
            recaps[display_name] = cached["recap_text"]
            continue

        # Don't recap while the most recent contributing session still
        # looks live — its summary (and thus the recap built from it) is
        # about to be stale anyway.
        if _is_live(sessions[0], now):
            if cached:
                recaps[display_name] = cached["recap_text"]
            continue

        to_regenerate.append((slug, summarized, display_name, signature))

    for slug, summarized, display_name, signature in to_regenerate[:PROJECT_RECAP_CAP]:
        recap_text = ss.run_project_recap(slug, summarized)
        if recap_text:
            _write_project_recap(slug, {
                "recap_text": recap_text,
                "generated_at": _iso(now),
                "based_on": signature,
            })
            recaps[display_name] = recap_text
        else:
            cached = _read_project_recap(slug)
            if cached:
                recaps[display_name] = cached["recap_text"]

    return recaps


def add_legacy_no_id_entries(data: list[dict], no_id_entries: list[LegacyEntry]) -> None:
    for e in no_id_entries:
        project = project_display_name(e.cwd, "unknown")
        summary = make_summary(e.worked_on, e.completed)
        title = make_title(summary, e.worked_on, project)
        next_required, next_optional = split_next_action(e.status, e.next_action)
        stopped_norm = e.stopped_at.strip().rstrip(".").lower()
        stopped_at_visible = None if (e.status == "Completed" and stopped_norm in ("", "nothing notable")) else e.stopped_at
        data.append(
            {
                "project": project,
                "cwd": e.cwd,
                "cwd_short": shorten_cwd(e.cwd),
                "ended_at": e.ended_at,
                "status": e.status,
                "title": title,
                "summary": summary,
                "worked_on": e.worked_on,
                "completed": e.completed,
                "stopped_at_visible": stopped_at_visible,
                "next_action_required": next_required,
                "next_action_optional": next_optional,
                "resume_command": None,
                "summary_source": "legacy_md",
                "summary_at": e.ended_at,
                "pending_label": None,
                "force_command": None,
            }
        )


def _relative_label(iso_ts: str | None) -> str:
    dt = _parse_ts(iso_ts)
    if dt is None:
        return "unknown time"
    delta = _now() - dt
    secs = delta.total_seconds()
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


def render_html(data: list[dict], project_recaps: dict[str, str] | None = None) -> str:
    data.sort(key=lambda d: d["ended_at"], reverse=True)
    data_json = json.dumps(data).replace("</", "<\\/")
    recaps_json = json.dumps(project_recaps or {}).replace("</", "<\\/")

    generated_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
    exclusion_note = (
        f" {len(EXCLUDED_PREFIXES)} project path(s) are excluded from this dashboard "
        f"(see ~/.claude/session-logs/recap-exclude.txt)."
        if EXCLUDED_PREFIXES else ""
    )

    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Claude Work Recap</title>
<style>
  :root {{
    color-scheme: light dark;
    --bg: #ffffff; --fg: #1a1a1a; --muted: #666; --border: #e0e0e0;
    --card-bg: #fafafa; --accent: #3457d5;
    --completed: #1a7f37; --in-progress: #9a6700; --blocked: #cf222e; --unknown: #57606a;
    --completed-bg: #e9f7ee; --in-progress-bg: #fdf2e0; --blocked-bg: #fdecea; --unknown-bg: #eef0f2;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #14161a; --fg: #e6e6e6; --muted: #9a9a9a; --border: #2a2d33;
      --card-bg: #1b1e24; --accent: #7c9cff;
      --completed-bg: #16261c; --in-progress-bg: #2a2115; --blocked-bg: #2b1a1a; --unknown-bg: #202329;
    }}
  }}
  * {{ box-sizing: border-box; }}
  body {{ background: var(--bg); color: var(--fg); font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
          margin: 0; padding: 0 16px 60px; }}
  .wrap {{ max-width: 1180px; margin: 0 auto; }}
  header.page-head {{ padding: 20px 0 12px; }}
  h1 {{ font-size: 1.4rem; margin: 0 0 4px; }}
  .subtitle {{ color: var(--muted); font-size: 0.85rem; }}
  .privacy-note {{ color: var(--muted); font-size: 0.78rem; margin: 4px 0 16px; line-height: 1.4; }}
  .privacy-note code {{ background: var(--card-bg); border: 1px solid var(--border); border-radius: 3px; padding: 0 4px; }}

  .overview {{ display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 16px; }}
  button.stat {{ flex: 1 1 150px; background: var(--card-bg); color: var(--fg); border: 1px solid var(--border);
           border-radius: 8px; padding: 10px 16px; text-align: left; cursor: pointer; font: inherit; }}
  button.stat:hover {{ border-color: var(--accent); }}
  button.stat[aria-pressed="true"] {{ border-color: var(--accent); border-width: 2px;
           background: color-mix(in srgb, var(--accent) 12%, var(--card-bg)); }}
  button.stat .num {{ font-size: 1.4rem; font-weight: 600; display: block; }}
  button.stat .label {{ font-size: 0.75rem; color: var(--muted); }}

  .controls {{ position: sticky; top: 0; z-index: 10; display: flex; gap: 8px; flex-wrap: wrap;
           align-items: center; padding: 10px 0; margin-bottom: 8px; background: var(--bg);
           border-bottom: 1px solid var(--border); }}
  input[type=text] {{ background: var(--card-bg); color: var(--fg); border: 1px solid var(--border);
           border-radius: 6px; padding: 8px 10px; font-size: 0.9rem; flex: 1; min-width: 180px; }}
  button.clear-filters {{ background: none; color: var(--accent); border: none; font-size: 0.85rem;
           cursor: pointer; padding: 8px 4px; text-decoration: underline; }}

  section.session-section {{ margin-bottom: 32px; }}
  section.session-section > h2 {{ font-size: 1.05rem; font-weight: 700; margin: 0 0 12px; }}

  .project-name {{ font-size: 1.05rem; font-weight: 700; }}
  .project-path {{ font-size: 0.75rem; color: var(--muted); font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}

  /* --- project grid (home view) --- */
  .project-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 14px; }}
  .project-card {{ display: block; width: 100%; text-align: left; background: var(--card-bg);
           border: 1px solid var(--border); border-radius: 12px; padding: 16px; cursor: pointer;
           font: inherit; color: var(--fg); }}
  .project-card:hover {{ border-color: var(--accent); }}
  .project-card-head {{ margin-bottom: 8px; }}
  .project-card-chips {{ font-size: 0.76rem; color: var(--unknown); font-weight: 600; margin-bottom: 10px; }}
  .project-recap {{ font-size: 0.82rem; color: var(--fg); line-height: 1.4; margin-bottom: 10px; }}
  .project-preview {{ display: flex; flex-direction: column; gap: 6px; margin-bottom: 10px; }}
  .preview-row {{ display: flex; align-items: center; gap: 7px; font-size: 0.82rem; }}
  .preview-dot {{ width: 8px; height: 8px; min-width: 8px; border-radius: 50%; }}
  .preview-dot.completed {{ background: var(--completed); }}
  .preview-dot.in-progress {{ background: var(--in-progress); }}
  .preview-dot.blocked {{ background: var(--blocked); }}
  .preview-dot.unknown {{ background: var(--unknown); }}
  .preview-title {{ flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .preview-date {{ color: var(--muted); font-size: 0.74rem; white-space: nowrap; }}
  .project-card-count {{ font-size: 0.74rem; color: var(--muted); border-top: 1px solid var(--border); padding-top: 8px; }}

  /* --- project detail view --- */
  .back-link {{ background: none; border: none; color: var(--accent); cursor: pointer; font: inherit;
           font-size: 0.85rem; padding: 6px 0; margin-bottom: 8px; text-decoration: underline; }}
  .detail-header {{ margin-bottom: 18px; padding-bottom: 12px; border-bottom: 2px solid var(--border); }}

  .card {{ background: var(--card-bg); border: 1px solid var(--border); border-left: 4px solid var(--unknown);
           border-radius: 10px; padding: 14px 16px; margin-bottom: 10px; }}
  .card.completed {{ border-left-color: var(--completed); }}
  .card.in-progress {{ border-left-color: var(--in-progress); }}
  .card.blocked {{ border-left-color: var(--blocked); }}
  .card-head {{ display: flex; justify-content: space-between; align-items: flex-start; gap: 10px; margin-bottom: 6px; }}
  .card-title {{ font-size: 0.98rem; font-weight: 700; margin: 0; overflow: hidden; text-overflow: ellipsis;
           white-space: nowrap; }}
  .card-meta {{ display: flex; flex-wrap: wrap; gap: 6px 10px; align-items: center; color: var(--muted);
           font-size: 0.78rem; margin-bottom: 8px; }}
  .card-meta .project-chip {{ font-weight: 600; color: var(--fg); }}
  .badge {{ font-size: 0.72rem; font-weight: 600; padding: 2px 9px; border-radius: 999px; white-space: nowrap; }}
  .badge.completed {{ color: var(--completed); background: var(--completed-bg); }}
  .badge.in-progress {{ color: var(--in-progress); background: var(--in-progress-bg); }}
  .badge.blocked {{ color: var(--blocked); background: var(--blocked-bg); }}
  .badge.unknown {{ color: var(--unknown); background: var(--unknown-bg); }}

  .card-summary {{ font-size: 0.9rem; line-height: 1.5; margin-bottom: 8px; max-width: 68ch; }}
  .card-next {{ font-size: 0.86rem; margin-bottom: 8px; }}
  .card-next .k {{ color: var(--accent); font-weight: 700; font-size: 0.72rem; text-transform: uppercase;
           letter-spacing: 0.05em; margin-right: 6px; }}
  .card-next.no-action {{ color: var(--muted); font-style: italic; }}
  .card-provenance {{ font-size: 0.74rem; color: var(--muted); margin-bottom: 10px; }}
  .card-pending {{ font-size: 0.8rem; color: var(--unknown); font-style: italic; margin-bottom: 10px; }}

  .card-actions {{ display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }}
  .btn {{ border-radius: 6px; padding: 6px 12px; font-size: 0.8rem; cursor: pointer; border: 1px solid transparent; }}
  .btn-primary {{ background: var(--accent); color: white; border-color: var(--accent); }}
  .btn-secondary {{ background: none; color: var(--fg); border-color: var(--border); }}
  .btn-primary.copied {{ background: var(--completed); border-color: var(--completed); }}

  .details {{ margin-top: 12px; padding-top: 12px; border-top: 1px solid var(--border); }}
  .field {{ margin-bottom: 14px; font-size: 0.88rem; line-height: 1.5; max-width: 68ch; }}
  .field:last-child {{ margin-bottom: 0; }}
  .field .k {{ color: var(--accent); font-size: 0.72rem; font-weight: 700; text-transform: uppercase;
           letter-spacing: 0.05em; display: block; margin-bottom: 5px; }}
  .field-text {{ margin: 0; }}
  ul.field-list {{ margin: 0; padding-left: 1.2em; }}
  ul.field-list li {{ margin-bottom: 4px; }}
  ul.field-list li:last-child {{ margin-bottom: 0; }}
  .field code, .field-text code, code.resume {{ background: rgba(127,127,127,0.15); padding: 1px 5px; border-radius: 4px;
           font-size: 0.85em; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
  code.resume {{ display: block; padding: 8px 10px; overflow-x: auto; white-space: nowrap; user-select: all; }}
  .no-resume {{ color: var(--muted); font-size: 0.78rem; font-style: italic; }}

  .empty {{ color: var(--muted); padding: 40px 0; text-align: center; }}
  [hidden] {{ display: none !important; }}

  button:focus-visible, input:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px; }}

  @media (max-width: 480px) {{
    .overview {{ display: grid; grid-template-columns: 1fr 1fr; }}
    button.stat {{ flex: none; }}
    .card {{ padding: 12px; }}
    .card-title {{ white-space: normal; }}
  }}
</style>
</head>
<body>
<div class="wrap">
  <header class="page-head">
    <h1>Claude Work Recap</h1>
    <div class="subtitle">Generated {generated_at} · dashboard data stays on this machine</div>
  </header>

  <div class="privacy-note">
    Session summaries and project recaps are generated by a local
    <code>claude -p</code> call, which sends that session's transcript to
    your configured Claude model to produce the summary text — this is the
    only network activity anywhere in this pipeline.{exclusion_note}
  </div>

  <div class="overview" id="overview"></div>

  <div class="controls">
    <input type="text" id="search" placeholder="Search project, summary, next action...">
    <button class="clear-filters" id="clearFilters" hidden>Clear filters</button>
  </div>

  <section id="view-grid">
    <div class="project-grid" id="project-grid"></div>
  </section>

  <section id="view-detail" hidden>
    <button type="button" class="back-link" id="backToGrid">← All projects</button>
    <div class="detail-header">
      <div class="project-name" id="detail-project-name"></div>
      <div class="project-path" id="detail-project-path"></div>
    </div>
    <div id="detail-results"></div>
  </section>

  <div class="empty" id="empty-state" hidden></div>
</div>

<script>
const SESSIONS = {data_json};
SESSIONS.forEach((s, i) => {{ s.idx = i; }});
const PROJECT_RECAPS = {recaps_json};

const SOURCE_LABEL = {{"session_end": "session end", "recap_reconciliation": "recap check", "legacy_md": "earlier log"}};

let searchQuery = "";
let overviewFilter = null; // null | "Completed" | "Unfinished" | "Blocked"
let currentView = "grid";  // "grid" | "detail"
let currentProject = null;
const expandedCards = new Set();

function getProjectGroups() {{
  const map = new Map();
  for (const s of SESSIONS) {{
    if (!map.has(s.project)) map.set(s.project, []);
    map.get(s.project).push(s);
  }}
  for (const sessions of map.values()) {{
    sessions.sort((a, b) => b.ended_at.localeCompare(a.ended_at));
  }}
  return map;
}}

function applyHashState() {{
  const params = new URLSearchParams(location.hash.replace(/^#/, ""));
  const project = params.get("project");
  if (project && SESSIONS.some(s => s.project === project)) {{
    currentView = "detail";
    currentProject = project;
  }} else {{
    currentView = "grid";
    currentProject = null;
  }}
}}

function el(tag, opts) {{
  const e = document.createElement(tag);
  opts = opts || {{}};
  if (opts.className) e.className = opts.className;
  if (opts.text !== undefined) e.textContent = opts.text;
  return e;
}}

function badgeClass(status) {{
  return {{
    "Completed": "completed",
    "In Progress": "in-progress",
    "Blocked": "blocked",
  }}[status] || "unknown";
}}

function escapeHtml(str) {{
  return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}}

// The only innerHTML usage in this file. `str` is escaped FIRST, so any
// user/session-derived `<`, `>`, `&` are neutralized before this ever runs;
// the regex then only ever wraps already-escaped text in a fixed, static
// <code>...</code> pair. This is a single narrow substitution, not markdown
// rendering, and cannot be used to inject arbitrary markup.
function inlineFormat(str) {{
  return escapeHtml(str).replace(/`([^`]+)`/g, "<code>$1</code>");
}}

function renderField(container, label, value) {{
  const f = el("div", {{className: "field"}});
  f.appendChild(el("span", {{className: "k", text: label}}));
  const lines = value.split("\\n").map(l => l.trim()).filter(l => l.length > 0);
  const isList = lines.length > 1 && lines.every(l => l.startsWith("- ") || l.startsWith("* "));
  if (isList) {{
    const ul = el("ul", {{className: "field-list"}});
    for (const line of lines) {{
      const li = document.createElement("li");
      li.innerHTML = inlineFormat(line.slice(2));
      ul.appendChild(li);
    }}
    f.appendChild(ul);
  }} else {{
    const div = el("div", {{className: "field-text"}});
    div.innerHTML = lines.map(inlineFormat).join("<br>");
    f.appendChild(div);
  }}
  container.appendChild(f);
  return f;
}}

function formatFriendlyDate(iso) {{
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  const now = new Date();
  const sameDay = (a, b) => a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  const timeStr = d.toLocaleTimeString(undefined, {{hour: "numeric", minute: "2-digit"}});
  if (sameDay(d, now)) return `Today, ${{timeStr}}`;
  if (sameDay(d, yesterday)) return `Yesterday, ${{timeStr}}`;
  const dateStr = d.toLocaleDateString(undefined, {{day: "numeric", month: "short", year: "numeric"}});
  return `${{dateStr}} · ${{timeStr}}`;
}}

function formatRelative(iso) {{
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "unknown time";
  const secs = (Date.now() - d.getTime()) / 1000;
  if (secs < 60) return "just now";
  if (secs < 3600) return Math.floor(secs / 60) + "m ago";
  if (secs < 86400) return Math.floor(secs / 3600) + "h ago";
  return Math.floor(secs / 86400) + "d ago";
}}

function formatTokens(n) {{
  if (n >= 1000) return (n / 1000).toFixed(1).replace(/\\.0$/, "") + "k";
  return String(n);
}}

function fallbackCopy(text, onDone) {{
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.focus();
  ta.select();
  try {{ document.execCommand("copy"); }} catch (e) {{ /* ignore */ }}
  document.body.removeChild(ta);
  onDone();
}}

function copyToClipboard(btn, text, doneLabel) {{
  const original = btn.textContent;
  const finish = () => {{
    btn.textContent = doneLabel;
    btn.classList.add("copied");
    setTimeout(() => {{ btn.textContent = original; btn.classList.remove("copied"); }}, 2200);
  }};
  if (navigator.clipboard && navigator.clipboard.writeText) {{
    navigator.clipboard.writeText(text).then(finish).catch(() => fallbackCopy(text, finish));
  }} else {{
    fallbackCopy(text, finish);
  }}
}}

function buildCard(s, opts) {{
  opts = opts || {{}};
  const card = el("div", {{className: "card " + badgeClass(s.status)}});

  const head = el("div", {{className: "card-head"}});
  head.appendChild(el("h3", {{className: "card-title", text: s.title}}));
  head.appendChild(el("span", {{className: "badge " + badgeClass(s.status), text: s.status}}));
  card.appendChild(head);

  const meta = el("div", {{className: "card-meta"}});
  if (opts.showProject) {{
    meta.appendChild(el("span", {{className: "project-chip", text: s.project}}));
  }}
  const when = el("span", {{text: formatFriendlyDate(s.ended_at)}});
  when.title = s.ended_at;
  meta.appendChild(when);
  card.appendChild(meta);

  card.appendChild(el("div", {{className: "card-summary", text: s.summary}}));

  if (s.summary_source) {{
    const label = SOURCE_LABEL[s.summary_source] || s.summary_source;
    card.appendChild(el("div", {{className: "card-provenance",
      text: `Summary: ${{label}} · ${{formatRelative(s.summary_at || s.ended_at)}}`}}));
  }} else if (s.pending_label) {{
    card.appendChild(el("div", {{className: "card-pending", text: s.pending_label}}));
  }}

  if (s.summary_source) {{
    const nextLine = el("div", {{className: "card-next" + (s.next_action_required ? "" : " no-action")}});
    if (s.next_action_required) {{
      nextLine.appendChild(el("span", {{className: "k", text: "Next"}}));
      nextLine.appendChild(document.createTextNode(s.next_action_required));
    }} else {{
      nextLine.textContent = "No required next action.";
    }}
    card.appendChild(nextLine);
  }}

  const actions = el("div", {{className: "card-actions"}});
  if (s.resume_command) {{
    const copyBtn = el("button", {{className: "btn btn-primary", text: "Copy resume command"}});
    copyBtn.addEventListener("click", () => copyToClipboard(copyBtn, s.resume_command, "Copied — paste into Terminal."));
    actions.appendChild(copyBtn);
  }}
  if (s.force_command) {{
    const forceBtn = el("button", {{className: "btn btn-secondary", text: "Copy summarize-now command"}});
    forceBtn.addEventListener("click", () => copyToClipboard(forceBtn, s.force_command, "Copied — paste into Terminal."));
    actions.appendChild(forceBtn);
  }}
  const detailsBtn = el("button", {{className: "btn btn-secondary", text: "View details"}});
  detailsBtn.setAttribute("aria-expanded", "false");
  actions.appendChild(detailsBtn);
  card.appendChild(actions);

  const details = el("div", {{className: "details"}});
  details.hidden = true;
  renderField(details, "Worked on", s.worked_on);
  renderField(details, "Completed", s.completed);
  if (s.stopped_at_visible) {{
    renderField(details, "Stopped at", s.stopped_at_visible);
  }}
  if (s.next_action_optional) {{
    renderField(details, "Optional follow-up", s.next_action_optional);
  }}
  if (s.context_peak_tokens != null || s.context_total_output_tokens != null) {{
    const usageField = el("div", {{className: "field"}});
    usageField.appendChild(el("span", {{className: "k", text: "Context usage (from transcript, approx.)"}}));
    const parts = [];
    if (s.context_peak_tokens != null) parts.push(`Peak context ~${{formatTokens(s.context_peak_tokens)}} tokens`);
    if (s.context_total_output_tokens != null) parts.push(`total output ~${{formatTokens(s.context_total_output_tokens)}} tokens`);
    usageField.appendChild(el("span", {{text: parts.join(" · ")}}));
    details.appendChild(usageField);
  }}
  if (s.resume_command) {{
    const resumeField = el("div", {{className: "field"}});
    resumeField.appendChild(el("span", {{className: "k", text: "Resume command"}}));
    resumeField.appendChild(el("code", {{className: "resume", text: s.resume_command}}));
    details.appendChild(resumeField);
  }} else {{
    details.appendChild(el("div", {{className: "no-resume", text: "No resume command available for this entry."}}));
  }}
  card.appendChild(details);

  const isExpanded = expandedCards.has(s.idx);
  details.hidden = !isExpanded;
  detailsBtn.textContent = isExpanded ? "Hide details" : "View details";
  detailsBtn.setAttribute("aria-expanded", String(isExpanded));

  detailsBtn.addEventListener("click", () => {{
    const nowHidden = !details.hidden;
    details.hidden = nowHidden;
    detailsBtn.textContent = nowHidden ? "View details" : "Hide details";
    detailsBtn.setAttribute("aria-expanded", String(!nowHidden));
    if (nowHidden) expandedCards.delete(s.idx); else expandedCards.add(s.idx);
  }});

  return card;
}}

function renderOverview() {{
  const completedCount = SESSIONS.filter(s => s.status === "Completed").length;
  const unfinishedCount = SESSIONS.filter(s => s.status === "In Progress" || s.status === "Unknown").length;
  const blockedCount = SESSIONS.filter(s => s.status === "Blocked").length;
  const attentionProjects = new Set(SESSIONS.filter(s => s.status !== "Completed").map(s => s.project));

  const stats = [
    {{label: "Needs attention", num: attentionProjects.size, value: null}},
    {{label: "Completed", num: completedCount, value: "Completed"}},
    {{label: "Unfinished", num: unfinishedCount, value: "Unfinished"}},
    {{label: "Blocked", num: blockedCount, value: "Blocked"}},
  ];

  const overview = document.getElementById("overview");
  overview.innerHTML = "";
  for (const stat of stats) {{
    const btn = el("button", {{className: "stat"}});
    btn.type = "button";
    btn.setAttribute("aria-pressed", String(overviewFilter === stat.value));
    btn.appendChild(el("span", {{className: "num", text: String(stat.num)}}));
    btn.appendChild(el("span", {{className: "label", text: stat.label}}));
    btn.addEventListener("click", () => {{
      overviewFilter = (overviewFilter === stat.value) ? null : stat.value;
      render();
    }});
    overview.appendChild(btn);
  }}
}}

function matchesOverviewFilter(s) {{
  if (overviewFilter === null) return true;
  if (overviewFilter === "Unfinished") return s.status === "In Progress" || s.status === "Unknown";
  return s.status === overviewFilter;
}}

function matchesSearch(s) {{
  if (!searchQuery) return true;
  const haystack = (s.project + " " + s.title + " " + s.summary + " " + s.worked_on + " " +
    (s.next_action_required || "")).toLowerCase();
  return haystack.includes(searchQuery);
}}

function renderClearFilters() {{
  const btn = document.getElementById("clearFilters");
  btn.hidden = !(searchQuery || overviewFilter !== null);
}}

function renderProjectGrid(filtered) {{
  const grid = document.getElementById("project-grid");
  grid.innerHTML = "";

  const matchedProjects = new Set(filtered.map(s => s.project));
  const groups = getProjectGroups();
  const projectNames = [...groups.keys()]
    .filter(p => matchedProjects.has(p))
    .sort((a, b) => groups.get(b)[0].ended_at.localeCompare(groups.get(a)[0].ended_at));

  if (projectNames.length === 0) return 0;

  for (const project of projectNames) {{
    const sessions = groups.get(project);
    const card = el("button", {{className: "project-card"}});
    card.type = "button";

    const head = el("div", {{className: "project-card-head"}});
    head.appendChild(el("div", {{className: "project-name", text: project}}));
    head.appendChild(el("div", {{className: "project-path", text: sessions[0].cwd_short}}));
    card.appendChild(head);

    const recap = PROJECT_RECAPS[project];
    if (recap) {{
      card.appendChild(el("div", {{className: "project-recap", text: recap}}));
    }}

    const counts = {{}};
    for (const s of sessions) {{
      if (s.status !== "Completed") counts[s.status] = (counts[s.status] || 0) + 1;
    }}
    const chipParts = ["Blocked", "In Progress", "Unknown"]
      .filter(status => counts[status])
      .map(status => `${{counts[status]}} ${{status.toLowerCase()}}`);
    if (chipParts.length) {{
      card.appendChild(el("div", {{className: "project-card-chips", text: chipParts.join(" · ")}}));
    }}

    const preview = el("div", {{className: "project-preview"}});
    for (const s of sessions.slice(0, 5)) {{
      const row = el("div", {{className: "preview-row"}});
      row.appendChild(el("span", {{className: "preview-dot " + badgeClass(s.status)}}));
      row.appendChild(el("span", {{className: "preview-title", text: s.title}}));
      row.appendChild(el("span", {{className: "preview-date", text: formatRelative(s.ended_at)}}));
      preview.appendChild(row);
    }}
    card.appendChild(preview);

    card.appendChild(el("div", {{className: "project-card-count",
      text: sessions.length + (sessions.length === 1 ? " session" : " sessions")}}));

    card.addEventListener("click", () => {{
      location.hash = "project=" + encodeURIComponent(project);
    }});

    grid.appendChild(card);
  }}
  return projectNames.length;
}}

function renderProjectDetail(filtered) {{
  const results = document.getElementById("detail-results");
  results.innerHTML = "";

  const groups = getProjectGroups();
  const allSessions = groups.get(currentProject) || [];
  document.getElementById("detail-project-name").textContent = currentProject || "";
  document.getElementById("detail-project-path").textContent = allSessions[0] ? allSessions[0].cwd_short : "";

  const sessions = filtered
    .filter(s => s.project === currentProject)
    .sort((a, b) => b.ended_at.localeCompare(a.ended_at));

  for (const s of sessions) {{
    results.appendChild(buildCard(s, {{showProject: false}}));
  }}
  return sessions.length;
}}

function render() {{
  renderClearFilters();

  const filtered = SESSIONS.filter(s => matchesOverviewFilter(s) && matchesSearch(s));

  document.getElementById("view-grid").hidden = currentView !== "grid";
  document.getElementById("view-detail").hidden = currentView !== "detail";

  const emptyState = document.getElementById("empty-state");
  let count;
  if (currentView === "grid") {{
    count = renderProjectGrid(filtered);
    emptyState.textContent = SESSIONS.length === 0 ? "No sessions logged yet." : "No projects match your filters.";
  }} else {{
    count = renderProjectDetail(filtered);
    emptyState.textContent = "No sessions match your filters in this project.";
  }}
  emptyState.hidden = count !== 0;
}}

document.getElementById("search").addEventListener("input", (e) => {{
  searchQuery = e.target.value.trim().toLowerCase();
  render();
}});
document.getElementById("clearFilters").addEventListener("click", () => {{
  searchQuery = "";
  overviewFilter = null;
  document.getElementById("search").value = "";
  render();
}});
document.getElementById("backToGrid").addEventListener("click", () => {{
  location.hash = "";
}});
window.addEventListener("hashchange", () => {{
  applyHashState();
  render();
}});

renderOverview();
applyHashState();
render();
</script>
</body>
</html>
"""


def generate_dashboard(force_session_id: str | None = None) -> Path:
    legacy_by_id, legacy_no_id = collect_legacy_entries()
    transcript_index = build_transcript_index()
    json_records = collect_json_records()
    combined = build_combined(legacy_by_id, json_records, transcript_index)
    reconcile_orphans(combined, transcript_index)
    fill_missing_cwd_from_transcripts(combined)

    run_lazy_summarization(combined, force_session_id=force_session_id)

    # Re-read: run_lazy_summarization wrote directly to disk via
    # session_record.py, so reload any records it touched. The transcript
    # index itself doesn't change (no session gets a new transcript file
    # mid-run), so it's reused as-is.
    json_records = collect_json_records()
    combined = build_combined(legacy_by_id, json_records, transcript_index)
    reconcile_orphans(combined, transcript_index)
    fill_missing_cwd_from_transcripts(combined)

    project_recaps = run_project_recaps(combined)

    data = build_session_data(combined)
    add_legacy_no_id_entries(data, legacy_no_id)

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(render_html(data, project_recaps))
    return OUT_FILE


def main() -> int:
    force_session_id = None
    args = sys.argv[1:]
    if args and args[0] == "--summarize-session":
        if len(args) < 2:
            print("Usage: session_dashboard.py --summarize-session <session_id>", file=sys.stderr)
            return 1
        force_session_id = args[1]

    out_path = generate_dashboard(force_session_id=force_session_id)
    print(str(out_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
