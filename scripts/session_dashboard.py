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

On every run, this script also backfills real summaries from deterministic,
size-bounded transcript evidence (via the same one-shot `claude -p` call used
by SessionEnd — see session_summarize.py). Model calls and aggregate input are
hard-capped per run, and project recaps are assembled locally with no extra
model call. Sessions that still look actively in use are skipped.
Status is NEVER inferred from a checkpoint's mere existence — a session
without a real summarizer-produced status is always shown as "Unknown",
honestly labeled with its last known activity.

Stdlib only except for the two sibling modules above. Prints the dashboard
path on stdout (default action), or use --summarize-session to force one
session's summary immediately, bypassing the per-run cap.
"""
from __future__ import annotations

import json
import re
import shlex
import sys
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


_OPT_OUT_CACHE: dict[str, bool] = {}


def _slug_cwd(slug: str) -> str | None:
    """Best-effort real cwd for a project slug (slugs are lossy, so it has
    to come from a record or a transcript)."""
    for json_file in sorted((LOG_ROOT / slug).glob("*.json")):
        if json_file.name.startswith("."):
            continue
        try:
            record = json.loads(json_file.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        for section in ("end", "start"):
            cwd = record.get(section, {}).get("cwd") if isinstance(record, dict) else None
            if cwd:
                return cwd
    for jsonl_file in sorted((PROJECTS_ROOT / slug).glob("*.jsonl")):
        cwd = read_cwd_from_transcript(jsonl_file)
        if cwd:
            return cwd
    return None


def _project_opted_out(slug: str) -> bool:
    if slug not in _OPT_OUT_CACHE:
        _OPT_OUT_CACHE[slug] = sr.project_opted_out(_slug_cwd(slug))
    return _OPT_OUT_CACHE[slug]


def _is_excluded_slug(slug: str) -> bool:
    if any(slug == p or slug.startswith(p + "-") for p in EXCLUDED_PREFIXES):
        return True
    return _project_opted_out(slug)

LAZY_SUMMARY_SCAN_CAP = 20    # newest stale sessions inspected for cache hits
MAX_SUMMARY_CALLS_PER_RUN = 2 # hard cap on paid/model-backed work per /recap
MAX_SUMMARY_INPUT_CHARS_PER_RUN = 80_000
LIVE_THRESHOLD_SECONDS = 300  # 5 minutes: "still looks in use, don't touch"
LONG_RUNNING_OPEN_SECONDS = 2 * 60 * 60
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

# Known low-information lead-ins that make_title() strips before truncating —
# e.g. "Ran the /recap skill to catch up on..." or "User asked for an
# explanation of...". Purely mechanical prefix removal on already-stored
# text: never invents content, never touches the underlying worked_on/summary
# fields, only how a title is DERIVED from them for display.
TITLE_LEADIN_RES = [
    re.compile(r"^ran\s+(the\s+)?/?[\w-]+\s+(skill|command|slash command)\b.{0,20}?\bto\s+", re.IGNORECASE),
    re.compile(r"^(the\s+)?user\s+asked\s+(for|to|whether|if)\s+", re.IGNORECASE),
    re.compile(r"^(the\s+)?user\s+requested\s+", re.IGNORECASE),
    re.compile(r"^(i\s+)?(was\s+asked\s+to|helped\s+(the\s+)?user)\s+", re.IGNORECASE),
]

# A session with no real timestamp gets a "1970-01-01T00:00:00Z" fallback
# (see build_session_data/load_legacy_entry) so sorting always has a value to
# compare against — but that fallback must never be presented to a human as
# a real date (e.g. "20709d ago"), and must never be treated as equally
# comparable to a genuine, if old, timestamp. Anything before this floor is
# certainly the fallback, not real session activity (this pipeline didn't
# exist before then).
PLAUSIBLE_TS_FLOOR_YEAR = 2020


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


def _is_plausible_ts(iso: str | None) -> bool:
    """True only for a timestamp that could plausibly be real session
    activity — not missing, not a parse failure, and not the epoch-era
    fallback used when nothing else is known. Used to gate both display
    ("Date unavailable" vs a real date) and sort order (a session/project
    with no real timestamp must never be treated as equally recent — or more
    recent — than one with a genuine, even old, timestamp)."""
    dt = _parse_ts(iso)
    if dt is None:
        return False
    if dt.year < PLAUSIBLE_TS_FLOOR_YEAR:
        return False
    if dt > _now() + timedelta(days=1):  # small clock-skew tolerance
        return False
    return True


def _as_text(value: object, fallback: str) -> str:
    """A summary-section field is supposed to be a string, but a
    hand-edited or corrupted record could hold anything JSON allows. Never
    let a wrong-typed value (a list, a dict, a number) reach code that
    assumes .strip()/.split() work on it."""
    if isinstance(value, str) and value.strip():
        return value
    return fallback


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

DASHBOARD_WARNINGS_LOG = LOG_ROOT / "dashboard-warnings.log"
KNOWN_RECORD_SECTIONS = ("start", "checkpoint", "end", "summary", "usage")

# Lives alongside <session_id>.json record files in the same project
# directory, but is a per-PROJECT cache (see _project_recap_path / one
# call site further down), never a session record — collect_json_records
# must not treat it as one.
PROJECT_RECAP_FILENAME = "project_recap.json"


def _log_dashboard_warning(message: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        DASHBOARD_WARNINGS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with DASHBOARD_WARNINGS_LOG.open("a") as f:
            f.write(f"[{ts}] {message}\n")
    except OSError:
        pass


def _sanitize_record(raw: object, source: str) -> dict | None:
    """A record file is only ever produced by session_record.py's own
    merge_section, which always writes a top-level dict of dict-valued
    sections — but a hand-edited, partially-written, or otherwise corrupted
    file could violate that. One such file must never take down dashboard
    generation for every other session/project, so this validates the shape
    and drops (with a logged warning, not silently) whatever doesn't hold:
    a non-dict top level, or a section whose value isn't itself a dict."""
    if not isinstance(raw, dict):
        _log_dashboard_warning(f"{source}: record is not a JSON object (got {type(raw).__name__}) — skipped")
        return None
    sanitized: dict = {}
    for key, value in raw.items():
        if key in KNOWN_RECORD_SECTIONS and not isinstance(value, dict):
            _log_dashboard_warning(f"{source}: section {key!r} is not an object (got {type(value).__name__}) — dropped")
            continue
        sanitized[key] = value
    return sanitized


def collect_json_records() -> dict[str, dict]:
    """session_id -> record dict ({"start": ..., "checkpoint": ..., "end": ...,
    "summary": ...}), scanning every ~/.claude/session-logs/<slug>/<id>.json."""
    records: dict[str, dict] = {}
    # session_id is assumed globally unique (it mirrors a real Claude Code
    # transcript filename, which can only ever live under one project
    # directory) — every downstream structure (this dict, `combined` in
    # build_combined, `SESSIONS` in the dashboard) is keyed on that
    # assumption. If it's ever violated (duplicated/copied record files, a
    # bug elsewhere), silently letting the last one found overwrite the
    # first would make a whole session vanish with zero trace. Track where
    # each session_id was first seen so a collision can be logged instead.
    first_seen: dict[str, Path] = {}
    if not LOG_ROOT.is_dir():
        return records
    for project_dir in sorted(LOG_ROOT.iterdir()):
        if not project_dir.is_dir() or project_dir.name == SUMMARIZER_SCRATCH_SLUG:
            continue
        if _is_excluded_slug(project_dir.name):
            continue
        for json_file in sorted(project_dir.glob("*.json")):
            # Only <session_id>.json files are session records. A project
            # directory also holds project_recap.json (a per-PROJECT cache,
            # not a session — see _project_recap_path/PROJECT_RECAP_FILENAME
            # below) and dot-prefixed atomic-write temp files (merge_section
            # and _write_project_recap both write via a `.{...}.tmp-<pid>.json`
            # + rename). Both used to be silently ingested here as if they
            # were session records — project_recap.json in particular has no
            # "session_id" of its own, so json_file.stem ("project_recap")
            # collided identically across every project, one phantom
            # "(unknown project)" session actually being one project's
            # cached recap text mistaken for a session summary.
            if json_file.name.startswith(".") or json_file.name == PROJECT_RECAP_FILENAME:
                continue
            session_id = json_file.stem
            try:
                raw = json.loads(json_file.read_text())
            except (OSError, json.JSONDecodeError) as e:
                _log_dashboard_warning(f"{json_file}: unreadable/invalid JSON ({e}) — skipped")
                continue
            sanitized = _sanitize_record(raw, str(json_file))
            if sanitized is None:
                continue
            if session_id in records:
                _log_dashboard_warning(
                    f"session_id {session_id!r} found under two different project "
                    f"directories ({first_seen[session_id]} and {json_file}) — "
                    "keeping the first, ignoring the second. session_id is assumed "
                    "globally unique; this suggests a duplicated/copied record file."
                )
                continue
            records[session_id] = sanitized
            first_seen[session_id] = json_file
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


_WHITESPACE_RE = re.compile(r"\s+")


def _last_user_prompt(path: Path, max_chars: int = 200) -> str | None:
    """Best-effort, zero-model-call peek at the last thing the user actually
    typed — for a live session that's too fresh to run the real summarizer
    on (see _is_live). Streams the transcript same as scan_transcript_usage
    (no whole-file read); reuses ss._text_blocks to skip tool_result echoes
    that Claude Code also logs under type "user". Returns None, never a
    guess, if no real user text is found."""
    last_text = None
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
                if d.get("type") != "user":
                    continue
                # Claude Code also logs harness-injected turns (a background
                # task-completion notification, a skill's loaded body) as
                # ordinary type="user" text — not just tool_result echoes.
                # turnOrigin is the structural field it uses to mark a turn
                # as actually typed by the person; anything else (missing,
                # "task_notification", etc.) is not a real prompt to show.
                if d.get("turnOrigin") != "human":
                    continue
                content = (d.get("message") or {}).get("content")
                for value in ss._text_blocks(content):
                    if value.strip():
                        last_text = value
    except OSError:
        return None
    if last_text is None:
        return None
    normalized = _WHITESPACE_RE.sub(" ", last_text).strip()
    if not normalized:
        return None
    return ss._clip(normalized, max_chars)


LIVE_SNAPSHOT_TAIL_BYTES = 64 * 1024
_FILE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def scan_live_snapshot(path: Path, tail_bytes: int = LIVE_SNAPSHOT_TAIL_BYTES) -> dict | None:
    """Zero-model-call picture of an open session, from the transcript tail
    only: the last typed prompt, the last assistant text, tool-call counts
    and recently edited files. Reads at most `tail_bytes`, so the cost is
    flat no matter how long the session is. Returns None if the tail has
    nothing usable; never guesses."""
    try:
        with path.open("rb") as f:
            size = f.seek(0, 2)
            start = max(0, size - tail_bytes)
            f.seek(start)
            raw = f.read()
    except OSError:
        return None
    lines = raw.decode("utf-8", errors="ignore").splitlines()
    if start > 0 and lines:
        lines = lines[1:]  # first line is likely cut mid-record

    prompt = None
    latest = None
    tools: dict[str, int] = {}
    files: list[str] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = d.get("message") or {}
        content = message.get("content")
        if d.get("type") == "user" and d.get("turnOrigin") == "human":
            for value in ss._text_blocks(content):
                if value.strip():
                    prompt = value
        elif d.get("type") == "assistant":
            for value in ss._text_blocks(content):
                if value.strip():
                    latest = value
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    name = str(block.get("name") or "")
                    if not name:
                        continue
                    tools[name] = tools.get(name, 0) + 1
                    target = (block.get("input") or {}).get("file_path")
                    if name in _FILE_TOOLS and isinstance(target, str):
                        base = Path(target).name
                        if base in files:
                            files.remove(base)
                        files.append(base)

    def tidy(text: str | None, limit: int) -> str | None:
        if text is None:
            return None
        text = _WHITESPACE_RE.sub(" ", text).strip()
        return ss._clip(text, limit) if text else None

    snapshot = {
        "prompt": tidy(prompt, 200),
        "latest": tidy(latest, 280),
        "tools": dict(sorted(tools.items(), key=lambda kv: -kv[1])[:5]),
        "files": files[-5:],
    }
    if not (snapshot["prompt"] or snapshot["latest"] or snapshot["tools"] or snapshot["files"]):
        return None
    return snapshot


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
    for ts in (
        checkpoint.get("checked_at"), end.get("ended_at"),
        start.get("_ts"), start.get("started_at"),
    ):
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


def _current_opened_at(c: Combined) -> datetime | None:
    """Latest open lifecycle event, including a resume after an earlier end."""
    start = c.record.get("start", {})
    end = c.record.get("end", {})
    started = _parse_ts(start.get("_ts") or start.get("started_at"))
    ended = _parse_ts(end.get("_ts") or end.get("ended_at"))
    if started is not None and (ended is None or started > ended):
        return started
    return None


def _has_ended(c: Combined) -> bool:
    if _current_opened_at(c) is not None:
        return False
    return bool(c.record.get("end")) or c.legacy is not None


def _long_running_open_label(c: Combined, now: datetime) -> str | None:
    opened_at = _current_opened_at(c)
    if opened_at is None:
        return None
    elapsed_seconds = max(0, int((now - opened_at).total_seconds()))
    if elapsed_seconds < LONG_RUNNING_OPEN_SECONDS:
        return None
    total_minutes = elapsed_seconds // 60
    hours, minutes = divmod(total_minutes, 60)
    return f"Open for {hours}h {minutes}m" if minutes else f"Open for {hours}h"


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


def _open_state(c: Combined, now: datetime) -> tuple[str | None, int]:
    """("live" | "idle" | None, idle_minutes) for an OPEN session. Separate
    from the summary status: it says whether the session is being used now,
    not what its work amounted to."""
    if _current_opened_at(c) is None:
        return None, 0
    last_active = _last_activity(c)
    if last_active is None:
        return None, 0
    idle_seconds = max(0, int((now - last_active).total_seconds()))
    state = "live" if idle_seconds < LIVE_THRESHOLD_SECONDS else "idle"
    return state, idle_seconds // 60


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
        prepared = ss.prepare_summary_evidence(str(c.transcript_path))
        if prepared is None:
            print(f"Could not extract evidence for session {force_session_id}.", file=sys.stderr)
            return
        slug = sr.slugify_cwd(c.cwd) if c.cwd else c.transcript_path.parent.name
        ss.run_bounded_summary(
            str(c.transcript_path), slug, force_session_id, "recap_reconciliation",
            timeout_secs=180, prepared=prepared,
        )
        return

    candidates = [
        c for c in combined.values()
        if _needs_summary(c) and not _is_live(c, now) and c.transcript_path is not None
    ]
    candidates.sort(key=lambda c: _last_activity(c) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    candidates = candidates[:LAZY_SUMMARY_SCAN_CAP]
    if not candidates:
        return

    model_calls = 0
    input_chars = 0
    for c in candidates:
        prepared = ss.prepare_summary_evidence(str(c.transcript_path))
        if prepared is None:
            continue
        slug = sr.slugify_cwd(c.cwd) if c.cwd else c.transcript_path.parent.name

        # Cache hits are free: run_bounded_summary only advances byte coverage
        # and never launches the model when evidence and prompt versions match.
        is_cache_hit = ss.summary_matches_evidence(c.record.get("summary"), prepared)
        if not is_cache_hit:
            if model_calls >= MAX_SUMMARY_CALLS_PER_RUN:
                continue
            if input_chars + prepared.input_chars > MAX_SUMMARY_INPUT_CHARS_PER_RUN:
                continue
            model_calls += 1
            input_chars += prepared.input_chars

        ss.run_bounded_summary(
            str(c.transcript_path), slug, c.session_id, "recap_reconciliation",
            timeout_secs=SUMMARY_TIMEOUT_SECS, prepared=prepared,
        )


# --- Display-only derived fields --------------------------------------------

def project_display_name(cwd: str | None, fallback_slug: str) -> str:
    if cwd:
        return Path(cwd).name or cwd
    return f"(unknown project — {fallback_slug})"


def build_resume_command(cwd: str | None, session_id: str | None) -> str | None:
    if not cwd or not session_id:
        return None
    return f"cd {shlex.quote(cwd)} && claude --resume {shlex.quote(session_id)}"


def resume_unavailable_reason(cwd: str | None, session_id: str | None) -> str | None:
    """Why build_resume_command() returned None, so the UI can disable the
    action with a specific explanation instead of a generic "unavailable"."""
    if not cwd and not session_id:
        return "no project path or session id on record"
    if not cwd:
        return "no project path on record"
    if not session_id:
        return "no session id on record"
    return None


def _strip_markdown_inline(text: str) -> str:
    text = re.sub(r"^[-*]\s+", "", text.strip())
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    return text.strip()


# A title/summary only ever needs a short excerpt (make_title further caps
# it to 10 words), so the sentence-boundary regex below is only ever run
# against a bounded prefix. Without this, a very long, punctuation-free
# field (a corrupted or adversarial `worked_on` value) makes
# `(.+?[.!?])(\s|$)` quadratic — re.search has to fail at every start
# position, each attempt itself scanning to the end — measured ~25x slower
# for 5x more input, hanging indefinitely on a multi-MB field.
FIRST_SENTENCE_SCAN_LIMIT = 500


def _first_sentence(text: str) -> str:
    text = text.strip()
    if not text:
        return ""
    first_line = text.splitlines()[0].strip()
    was_truncated = len(first_line) > FIRST_SENTENCE_SCAN_LIMIT
    first_line = _strip_markdown_inline(first_line[:FIRST_SENTENCE_SCAN_LIMIT])
    m = re.search(r"(.+?[.!?])(\s|$)", first_line)
    if m:
        sentence = m.group(1)
    elif was_truncated:
        sentence = first_line + "…"
    else:
        sentence = first_line
    return sentence.strip()


def make_summary(worked_on: str, completed: str) -> str:
    for candidate in (worked_on, completed):
        norm = candidate.strip().rstrip(".").lower()
        if norm and norm != "nothing notable":
            sentence = _first_sentence(candidate)
            if sentence:
                return sentence
    return "No summary available."


def _strip_title_leadin(basis: str) -> str:
    """Strip a known low-information lead-in ("Ran the /recap skill to...",
    "User asked for...") so the title starts at the actual substance instead
    of restating that a skill ran or a question was asked. Falls back to the
    untouched basis if stripping would leave too little to make a title from
    — never truncates into a meaningless fragment."""
    for pattern in TITLE_LEADIN_RES:
        stripped = pattern.sub("", basis, count=1)
        if stripped != basis and len(stripped.split()) >= 3:
            return stripped[:1].upper() + stripped[1:]
    return basis


def make_title(summary: str, worked_on: str, project: str) -> str:
    basis = summary if summary and summary not in ("No summary available.", "Not yet summarized.") else _first_sentence(worked_on)
    basis = basis.rstrip(".!?")
    basis = _strip_title_leadin(basis)
    words = basis.split()
    if not words:
        return f"{project} session"
    title_words = words[:16]
    title = " ".join(title_words)
    if len(words) > 16:
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
        long_running_label = _long_running_open_label(c, now)

        if summary_section:
            # .get() with the same fallbacks parse_summary_body() itself uses
            # (session_summarize.py) — a hand-edited or partially-written
            # summary section missing a key, or one with a wrong-typed value
            # (e.g. a list where a string is expected), must degrade
            # gracefully, not take down the whole dashboard with a KeyError
            # or AttributeError deeper in make_summary/make_title.
            status = summary_section.get("status")
            status = status if status in VALID_STATUSES else "Unknown"
            worked_on = _as_text(summary_section.get("worked_on"), "Nothing notable.")
            completed = _as_text(summary_section.get("completed"), "Nothing notable.")
            stopped_at = _as_text(summary_section.get("stopped_at"), "Nothing notable.")
            next_action_text = _as_text(summary_section.get("next_action"), "")
            summary_source = summary_section.get("summary_source")
            summary_at = summary_section.get("summary_at")
            summary = make_summary(worked_on, completed)
            title = make_title(summary, worked_on, project)
            pending_label = None
            last_prompt = None
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
                last_prompt = (
                    _last_user_prompt(c.transcript_path)
                    if c.transcript_path is not None
                    else None
                )
            else:
                pending_label = "Queued — will summarize on a later /recap run."
                last_prompt = None

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

        open_state, idle_minutes = _open_state(c, now)
        live_snapshot = (
            scan_live_snapshot(c.transcript_path)
            if open_state is not None and c.transcript_path is not None
            else None
        )
        behind_bytes = None
        if open_state is not None and c.record.get("summary"):
            known = _current_known_bytes(c)
            covered = c.record["summary"].get("summarized_through_bytes")
            if known is not None and isinstance(covered, int) and known > covered:
                behind_bytes = known - covered

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
                "resume_unavailable_reason": resume_unavailable_reason(cwd, c.session_id),
                "summary_source": summary_source,
                "summary_at": summary_at,
                "pending_label": pending_label,
                "last_prompt": last_prompt,
                "force_command": force_command,
                "long_running_open": long_running_label is not None,
                "long_running_label": long_running_label,
                "live_state": open_state,
                "idle_minutes": idle_minutes,
                "live_snapshot": live_snapshot,
                "summary_behind_bytes": behind_bytes,
                "last_activity_valid": _is_plausible_ts(last_active_iso),
                "context_peak_tokens": usage_stats["peak_context_tokens"] if usage_stats else None,
                "context_total_output_tokens": usage_stats["total_output_tokens"] if usage_stats else None,
            }
        )

    # No-session-id legacy entries (very old format) can't be deduped or
    # cross-checked against transcripts; show as before.
    return data


# --- Deterministic project-level recap --------------------------------------

PROJECT_RECAP_CANDIDATE_COUNT = 5


def _effective_slug(c: Combined) -> str | None:
    if c.transcript_path is not None:
        return c.transcript_path.parent.name
    if c.cwd:
        return sr.slugify_cwd(c.cwd)
    return None


def _session_summary_for_recap(c: Combined) -> dict | None:
    """Return only real session summaries, never pending placeholders."""
    summary = c.record.get("summary")
    if not summary and c.legacy and not c.legacy.is_placeholder:
        summary = {
            "status": c.legacy.status,
            "worked_on": c.legacy.worked_on,
            "completed": c.legacy.completed,
            "next_action": c.legacy.next_action,
            "summary_at": c.legacy.ended_at,
        }
    if not summary:
        return None
    return {
        "session_id": c.session_id,
        "status": summary.get("status", "Unknown"),
        "worked_on": summary.get("worked_on", ""),
        "completed": summary.get("completed", ""),
        "next_action": summary.get("next_action", ""),
        "summary_at": summary.get("summary_at", ""),
    }


def _recap_fragment(summary: dict) -> str | None:
    for field in ("completed", "worked_on"):
        value = _as_text(summary.get(field), "")
        if value.strip().rstrip(".").lower() == "nothing notable":
            continue
        fragment = _first_sentence(value).strip().rstrip(".!?")
        if fragment:
            return fragment[:240].rstrip()
    return None


def deterministic_project_recap(summaries: list[dict]) -> str | None:
    """Build a stable project recap from existing session summaries only."""
    subjects: list[str] = []
    next_action: str | None = None
    for summary in summaries:
        fragment = _recap_fragment(summary)
        if fragment and fragment.casefold() not in {item.casefold() for item in subjects}:
            subjects.append(fragment)

        raw_next = _as_text(summary.get("next_action"), "").strip()
        if next_action is None and raw_next and not NO_ACTION_RE.match(raw_next):
            required, _optional = split_next_action(str(summary.get("status", "Unknown")), raw_next)
            if required and not NO_ACTION_RE.match(required):
                next_action = _first_sentence(required).strip().rstrip(".!?")[:240]

    if not subjects:
        return None
    if len(subjects) == 1:
        recap = f"Recent work: {subjects[0]}."
    else:
        recap = f"Recent work spans: {subjects[0]}; {subjects[1]}."
    if next_action:
        recap += f" Next: {next_action}."
    return recap


def run_project_recaps(combined: dict[str, Combined]) -> dict[str, str]:
    """Build project recaps locally with no additional model calls."""
    by_slug: dict[str, list[Combined]] = {}
    for c in combined.values():
        slug = _effective_slug(c)
        if slug is None or slug == SUMMARIZER_SCRATCH_SLUG:
            continue
        by_slug.setdefault(slug, []).append(c)

    recaps: dict[str, str] = {}
    for slug, sessions in by_slug.items():
        sessions.sort(key=lambda c: _last_activity(c) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        summarized = [_session_summary_for_recap(c) for c in sessions]
        summarized = [s for s in summarized if s][:PROJECT_RECAP_CANDIDATE_COUNT]
        if not summarized:
            continue

        rep_cwd = next((c.cwd for c in sessions if c.cwd), None)
        display_name = project_display_name(rep_cwd, slug)
        recap_text = deterministic_project_recap(summarized)
        if recap_text:
            recaps[display_name] = recap_text

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
                "resume_unavailable_reason": resume_unavailable_reason(e.cwd, None),
                "summary_source": "legacy_md",
                "summary_at": e.ended_at,
                "pending_label": None,
                "force_command": None,
                "long_running_open": False,
                "long_running_label": None,
                "live_state": None,
                "idle_minutes": 0,
                "live_snapshot": None,
                "summary_behind_bytes": None,
                "last_activity_valid": _is_plausible_ts(e.ended_at),
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
    # last_activity_valid first so a session with no real timestamp always
    # sorts after every session with a genuine one, never intermixed by a
    # plain string compare on ended_at (which would otherwise put the
    # 1970-01-01 fallback last only by coincidence of string ordering).
    data.sort(key=lambda d: (d["last_activity_valid"], d["ended_at"]), reverse=True)
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
<title>recap. · Projects</title>
<meta name="color-scheme" content="dark">
<style>
  :root {{
    color-scheme: dark;
    --bg: #101318; --surface: #191e25; --surface-hover: #1d242c;
    --line: #2b323c; --line-soft: #262c35;
    --fg: #ecf0f4; --muted: #a0aab8; --dim: #8b95a4;
    --border: #2b323c; --card-bg: #191e25;
    --accent: #68dec3; --accent-bg: #192e2b;
    --completed: #2ea06a; --in-progress: #c9922e; --blocked: #e5534b; --unknown: #7d8896; --active: #4c9aff;
    --completed-bg: #16261c; --in-progress-bg: #2a2115; --blocked-bg: #2b1a1a; --unknown-bg: #202329; --active-bg: #172a3a;
    --mono: "IBM Plex Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    --font: "DM Sans", system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  }}
  * {{ box-sizing: border-box; }}
  html {{ scroll-behavior: smooth; }}
  body {{ background: var(--bg); color: var(--fg); font: 1rem/1.55 var(--font); margin: 0;
          -webkit-font-smoothing: antialiased; }}
  ::selection {{ background: #68dec340; color: #fff; }}
  .wrap {{ max-width: 1424px; margin: 0 auto; padding: 40px 48px 64px; }}

  .app-header {{ border-bottom: 1px solid var(--line-soft); background: #12161b; }}
  .header-inner {{ max-width: 1424px; height: 78px; padding: 0 48px; display: flex; align-items: center;
           gap: 26px; margin: 0 auto; }}
  .brand {{ display: flex; align-items: center; gap: 12px; font-size: 1.6rem; font-weight: 700;
           letter-spacing: -.065em; color: var(--fg); text-decoration: none; }}
  .brand-mark {{ position: relative; width: 32px; height: 32px; display: grid; place-items: center;
           color: var(--accent); background: #21322f; font: 700 22px/1 var(--font); border-radius: 9px;
           letter-spacing: -.05em; }}
  .brand-mark span {{ position: absolute; width: 4px; height: 4px; background: var(--accent);
           bottom: 5px; right: 5px; border-radius: 1px; }}
  .brand-period {{ color: var(--accent); }}
  .header-divider {{ height: 20px; width: 1px; background: var(--line); }}
  .workspace-label {{ font-size: .875rem; color: var(--muted); }}
  .private-label {{ margin-left: auto; display: flex; align-items: center; gap: 7px; font-size: .8125rem;
           color: var(--muted); }}
  .private-label svg {{ width: 14px; height: 14px; fill: none; stroke: currentColor; stroke-width: 1.6;
           stroke-linecap: round; stroke-linejoin: round; }}

  .eyebrow {{ font: 500 .75rem/1.5 var(--mono); color: var(--accent); letter-spacing: .12em;
           text-transform: uppercase; margin: 0 0 9px; }}
  .page-head {{ display: flex; justify-content: space-between; align-items: flex-end; gap: 24px;
           margin-bottom: 26px; }}
  h1 {{ font-size: 2.5rem; line-height: 1.15; letter-spacing: -.05em; font-weight: 600; margin: 0 0 11px; }}
  .subtitle {{ color: var(--muted); max-width: 720px; }}
  .overview-meta {{ display: flex; align-items: center; gap: 16px; color: var(--muted); font-size: .875rem;
           white-space: nowrap; padding-bottom: 5px; }}
  .overview-meta strong {{ color: var(--fg); font-weight: 600; }}
  .overview-meta .slash {{ color: var(--line); }}

  .how-summaries-work {{ color: var(--muted); font-size: .8125rem; line-height: 1.5; margin: 0 0 24px;
           border: 1px solid #31423f; background: #172522; border-radius: 9px; padding: 12px 16px; }}
  .how-summaries-work summary {{ cursor: pointer; color: #d0eee5; font-weight: 600; list-style: none; }}
  .how-summaries-work summary::-webkit-details-marker {{ display: none; }}
  .how-summaries-work summary::before {{ content: "▸ "; color: var(--accent); }}
  .how-summaries-work[open] summary::before {{ content: "▾ "; }}
  .how-summaries-work p {{ margin: 8px 0 0; }}
  .how-summaries-work code {{ background: var(--surface); border: 1px solid var(--line); border-radius: 3px;
           padding: 0 4px; font-family: var(--mono); }}

  .controls {{ display: flex; justify-content: space-between; align-items: center; gap: 20px;
           margin-bottom: 23px; }}
  .search-box {{ position: relative; max-width: 440px; width: 100%; display: flex; align-items: center; }}
  .search-box > svg {{ position: absolute; left: 15px; width: 18px; height: 18px; color: var(--dim);
           fill: none; stroke: currentColor; stroke-width: 1.6; stroke-linecap: round; pointer-events: none; }}
  input[type=text] {{ width: 100%; background: #171c22; color: var(--fg); border: 1px solid #343d47;
           border-radius: 9px; padding: 12px 16px 12px 44px; font: inherit; font-size: .875rem; line-height: 1.6; }}
  input[type=text]::placeholder {{ color: var(--dim); }}
  input[type=text]:focus {{ border-color: var(--accent); }}
  button.clear-filters {{ background: none; color: var(--accent); border: none; font: inherit; font-size: .85rem;
           cursor: pointer; padding: 8px 4px; text-decoration: underline; white-space: nowrap; }}
  .controls-right {{ display: flex; align-items: center; gap: 16px; }}
  .sort-label {{ display: flex; gap: 8px; align-items: center; color: var(--muted); font-size: .8125rem;
           white-space: nowrap; }}
  .sort-label svg {{ width: 17px; height: 17px; fill: none; stroke: currentColor; stroke-width: 1.6;
           stroke-linecap: round; }}

  section.session-section {{ margin-bottom: 32px; }}
  section.session-section > h2 {{ font-size: 1.05rem; font-weight: 700; margin: 0 0 12px; }}

  .project-name {{ font-size: 1.2rem; line-height: 1.4; font-weight: 600; letter-spacing: -.025em; }}
  .project-path {{ font: .75rem/1.6 var(--mono); color: var(--dim); overflow-wrap: anywhere; }}

  /* --- project grid (home view) --- */
  .project-grid {{ display: grid; grid-template-columns: 1fr; gap: 20px; align-items: stretch; }}
  @media (min-width: 760px) {{ .project-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }} }}
  @media (min-width: 1200px) {{ .project-grid {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }} }}
  .project-card {{ display: flex; flex-direction: column; width: 100%; min-width: 0; text-align: left;
           background: var(--surface); border: 1px solid var(--line); border-radius: 12px; padding: 0;
           overflow: hidden; cursor: pointer; font: inherit; color: var(--fg);
           transition: border-color .18s, transform .18s, box-shadow .18s; }}
  .project-card:hover {{ border-color: #4a5868; transform: translateY(-2px); box-shadow: 0 10px 28px #0002; }}
  .project-intro {{ padding: 24px 23px 19px; }}
  .project-top {{ display: flex; align-items: center; gap: 12px; margin-bottom: 12px; }}
  .project-icon {{ width: 40px; height: 40px; display: grid; place-items: center; border-radius: 10px;
           flex-shrink: 0; font-weight: 650; font-size: 1.05rem; text-transform: uppercase;
           color: hsl(var(--chip-hue) 70% 72%); background: hsl(var(--chip-hue) 40% 20%); }}
  .project-card-head {{ min-width: 0; }}
  .project-card-activity {{ font-size: .75rem; color: var(--dim); margin-top: 1px; }}
  .project-recap {{ color: #c7ced7; font-size: 1rem; line-height: 1.6; margin: 16px 0 0; min-height: 3.2em;
           display: -webkit-box; -webkit-line-clamp: 2; line-clamp: 2; -webkit-box-orient: vertical;
           overflow: hidden; }}
  .project-card-next {{ font-size: .875rem; color: var(--muted); line-height: 1.55;
           border-left: 2px solid #49645c; margin: 0 23px 21px; padding: 0 0 0 11px; min-height: 3.1em;
           display: -webkit-box; -webkit-line-clamp: 2; line-clamp: 2; -webkit-box-orient: vertical;
           overflow: hidden; }}
  .project-card-next .k {{ color: #c6e8dd; font-weight: 550; margin-right: 5px; }}
  .project-card-next.no-action {{ font-style: italic; }}
  .recent-header {{ display: flex; justify-content: space-between; color: var(--dim); font: .75rem var(--mono);
           letter-spacing: .08em; margin: 0 23px 8px; text-transform: uppercase; }}
  .project-preview {{ display: flex; flex-direction: column; margin: 0 23px 12px; }}
  .preview-row {{ display: flex; align-items: flex-start; gap: 11px; padding: 11px 0;
           border-top: 1px solid var(--line-soft); font-size: .875rem; line-height: 1.4; min-height: 59px; }}
  .project-card:hover .preview-row:hover .preview-title {{ color: var(--accent); }}
  .preview-dot {{ width: 8px; height: 8px; min-width: 8px; border-radius: 50%; margin-top: 6px; }}
  .preview-dot.completed {{ background: var(--completed); }}
  .preview-dot.in-progress {{ background: var(--in-progress); }}
  .preview-dot.blocked {{ background: var(--blocked); }}
  .preview-dot.unknown {{ background: var(--unknown); }}
  .preview-dot.active {{ background: var(--active); }}
  .preview-title {{ flex: 1; min-width: 0; display: -webkit-box; -webkit-line-clamp: 2; line-clamp: 2;
           -webkit-box-orient: vertical; overflow: hidden; transition: color .15s; }}
  .preview-date {{ font: .75rem/1.7 var(--mono); color: var(--dim); white-space: nowrap; flex-shrink: 0;
           padding-top: 1px; }}
  .project-foot {{ display: flex; justify-content: space-between; align-items: center; margin-top: auto;
           padding: 15px 23px; border-top: 1px solid var(--line); background: #161c22; }}
  .view-all-row {{ appearance: none; background: none; border: none; padding: 0; font: inherit;
           font-size: .875rem; font-weight: 500; color: #c7ded7; cursor: pointer; text-align: left; }}
  .view-all-row:hover {{ color: var(--accent); }}
  .project-foot-note {{ color: var(--dim); font: .75rem var(--mono); }}

  /* --- project detail view --- */
  .back-link {{ background: none; border: none; color: var(--accent); cursor: pointer; font: inherit;
           font-size: 0.85rem; padding: 6px 0; margin-bottom: 8px; text-decoration: underline; }}
  .detail-header {{ margin-bottom: 22px; padding-bottom: 14px; border-bottom: 1px solid var(--line); }}

  .card {{ background: var(--card-bg); border: 1px solid var(--border); border-left: 4px solid var(--unknown);
           border-radius: 12px; padding: 14px 16px; margin-bottom: 10px; }}
  .card.completed {{ border-left-color: var(--completed); background: var(--completed-bg); }}
  .card.in-progress {{ border-left-color: var(--in-progress); background: var(--in-progress-bg); }}
  .card.blocked {{ border-left-color: var(--blocked); background: var(--blocked-bg); }}
  .card.unknown {{ background: var(--unknown-bg); }}
  .card.active {{ border-left-color: var(--active); background: var(--active-bg); }}
  .card-head {{ display: flex; justify-content: space-between; align-items: flex-start; gap: 10px; margin-bottom: 6px; }}
  .card-title {{ font-size: 0.98rem; font-weight: 700; margin: 0; display: -webkit-box;
           -webkit-line-clamp: 2; line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }}
  .card-meta {{ display: flex; flex-wrap: wrap; gap: 6px 10px; align-items: center; color: var(--muted);
           font-size: 0.78rem; margin-bottom: 8px; }}
  .card-meta .project-chip {{ font-weight: 600; padding: 2px 8px; border-radius: 6px;
           color: hsl(var(--chip-hue) 65% 32%); background: hsl(var(--chip-hue) 65% 32% / 14%); }}
  .card-meta .project-chip {{ color: hsl(var(--chip-hue) 65% 72%); background: hsl(var(--chip-hue) 65% 72% / 16%); }}
  .badge {{ font-size: 0.72rem; font-weight: 600; padding: 2px 9px; border-radius: 999px; white-space: nowrap; }}
  .badge.completed {{ color: #fff; background: var(--completed); }}
  .badge.in-progress {{ color: #fff; background: var(--in-progress); }}
  .badge.blocked {{ color: #fff; background: var(--blocked); }}
  .badge.unknown {{ color: #fff; background: var(--unknown); }}
  .badge.active {{ color: #fff; background: var(--active); }}

  .card-summary {{ font-size: 0.9rem; line-height: 1.5; margin-bottom: 8px; max-width: 68ch; }}
  .card-next {{ font-size: 0.86rem; margin-bottom: 8px; }}
  .card-next .k {{ color: var(--accent); font-weight: 700; font-size: 0.72rem; text-transform: uppercase;
           letter-spacing: 0.05em; margin-right: 6px; }}
  .card-next.no-action {{ color: var(--muted); font-style: italic; }}
  .card-provenance {{ font-size: 0.74rem; color: var(--muted); margin-bottom: 10px; }}
  .card-pending {{ font-size: 0.8rem; color: var(--unknown); font-style: italic; margin-bottom: 10px; }}
  .card-live {{ font-size: 0.8rem; color: var(--muted); margin-bottom: 10px; display: grid; gap: 2px; }}
  .card-last-prompt {{ font-size: 0.8rem; color: var(--muted); margin-bottom: 10px; }}

  .card-actions {{ display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }}
  .btn {{ border-radius: 6px; padding: 6px 12px; font-size: 0.8rem; cursor: pointer; border: 1px solid transparent; }}
  .btn-primary {{ background: var(--accent); color: #10241f; font-weight: 600; border-color: var(--accent); }}
  .btn-secondary {{ background: none; color: var(--fg); border-color: var(--border); }}
  .btn-primary.copied {{ background: var(--completed); border-color: var(--completed); }}
  .btn:disabled {{ cursor: not-allowed; opacity: 0.5; }}

  .details {{ margin-top: 12px; padding-top: 12px; border-top: 1px solid var(--border); }}
  .field {{ margin-bottom: 14px; font-size: 0.88rem; line-height: 1.5; max-width: 68ch; }}
  .field:last-child {{ margin-bottom: 0; }}
  .field .k {{ color: var(--accent); font-size: 0.72rem; font-weight: 700; text-transform: uppercase;
           letter-spacing: 0.05em; display: block; margin-bottom: 5px; }}
  .field-text {{ margin: 0; }}
  ul.field-list {{ margin: 0; padding-left: 1.2em; }}
  ul.field-list li {{ margin-bottom: 4px; }}
  ul.field-list li:last-child {{ margin-bottom: 0; }}
  ul.field-list li {{ list-style: none; }}
  .claim-tag {{ display: inline-block; font-size: 0.68rem; font-weight: 700; text-transform: uppercase;
           letter-spacing: 0.03em; padding: 1px 7px; border-radius: 999px; margin-right: 6px; vertical-align: middle; }}
  .claim-tag.verified {{ color: var(--completed); background: var(--completed-bg); }}
  .claim-tag.discussed {{ color: var(--in-progress); background: var(--in-progress-bg); }}
  .claim-tag.uncertain {{ color: var(--unknown); background: var(--unknown-bg); }}
  .claim-evidence {{ display: block; font-size: 0.78rem; color: var(--muted); margin: 2px 0 0 2px; }}
  .field code, .field-text code, code.resume {{ background: rgba(127,127,127,0.15); padding: 1px 5px; border-radius: 4px;
           font-size: 0.85em; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
  code.resume {{ display: block; padding: 8px 10px; overflow-x: auto; white-space: nowrap; user-select: all; }}
  .no-resume {{ color: var(--muted); font-size: 0.78rem; font-style: italic; }}

  .empty {{ color: var(--muted); padding: 40px 0; text-align: center; }}
  [hidden] {{ display: none !important; }}

  @media (prefers-reduced-motion: reduce) {{
    * {{ animation-duration: 0.01ms !important; animation-iteration-count: 1 !important;
         transition-duration: 0.01ms !important; scroll-behavior: auto !important; }}
  }}

  button:focus-visible, input:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px; }}

  @media (max-width: 800px) {{
    .header-inner {{ padding: 0 24px; }}
    .wrap {{ padding: 28px 24px 48px; }}
    .page-head {{ display: block; }}
    .overview-meta {{ margin-top: 14px; }}
    h1 {{ font-size: 2rem; }}
    .controls {{ flex-direction: column; align-items: flex-start; gap: 14px; }}
  }}
  @media (max-width: 620px) {{
    .header-inner {{ height: 67px; gap: 18px; }}
    .header-divider, .workspace-label {{ display: none; }}
  }}
  @media (max-width: 480px) {{
    .card {{ padding: 12px; }}
  }}
</style>
</head>
<body>
<header class="app-header">
  <div class="header-inner">
    <a class="brand" href="#" aria-label="recap projects"><span class="brand-mark" aria-hidden="true">R<span></span></span><span>recap<span class="brand-period">.</span></span></a>
    <div class="header-divider" aria-hidden="true"></div><span class="workspace-label">Your workspace</span>
    <span class="private-label"><svg aria-hidden="true" viewBox="0 0 24 24"><rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V6a4 4 0 0 1 8 0v4"/></svg>Private workspace</span>
  </div>
</header>
<div class="wrap">
  <div class="page-head" id="overview-head">
    <div>
      <p class="eyebrow">Pick up the thread</p>
      <h1>Your projects</h1>
      <div class="subtitle">What changed. Where you left off. What comes next.</div>
    </div>
    <div class="overview-meta" id="header-stats">Generated {generated_at}</div>
  </div>

  <details class="how-summaries-work">
    <summary>How summaries work</summary>
    <p>
      Dashboard data (this page, and the session records behind it) is stored
      locally on this machine. Producing a session summary runs a local
      <code>claude -p</code> call, which may send up to 40,000 characters of
      deterministically filtered session evidence to your configured Claude
      model. Project recaps are assembled locally and make no additional model
      calls. Summary generation is the only network activity in this pipeline.{exclusion_note}
    </p>
  </details>

  <div class="controls">
    <div class="search-box">
      <svg aria-hidden="true" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>
      <input type="text" id="search" placeholder="Filter projects by name..." aria-label="Filter projects">
    </div>
    <div class="controls-right">
      <button class="clear-filters" id="clearFilters" hidden>Clear filters</button>
      <span class="sort-label"><svg aria-hidden="true" viewBox="0 0 24 24"><path d="M4 7h16M4 12h10M4 17h5"/></svg>Most recent first</span>
    </div>
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

const SOURCE_LABEL = {{"session_end": "session end", "recap_reconciliation": "recap check", "live": "live update", "legacy_md": "earlier log"}};
const GENERATED_AT = "{generated_at}";

let searchQuery = "";
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

// Deterministic hash of a project name to a stable 0-359 hue, so the same
// project always gets the same chip color across renders and machines.
function projectHue(name) {{
  let hash = 0;
  for (let i = 0; i < name.length; i++) {{
    hash = (hash * 31 + name.charCodeAt(i)) | 0;
  }}
  return Math.abs(hash) % 360;
}}

function badgeClass(session) {{
  if (session.long_running_open || session.live_state) return "active";
  return {{
    "Completed": "completed",
    "In Progress": "in-progress",
    "Blocked": "blocked",
  }}[session.status] || "unknown";
}}

function badgeLabel(session) {{
  if (session.long_running_open) return session.long_running_label;
  if (session.live_state === "live") return "Live";
  if (session.live_state === "idle") return `Open, idle ${{session.idle_minutes}}m`;
  return session.status;
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

const CLAIM_TAGS = {{
  "[Verified]": {{className: "verified", label: "Verified"}},
  "[Discussed]": {{className: "discussed", label: "Discussed"}},
  "[Uncertain]": {{className: "uncertain", label: "Uncertain"}},
}};

// Splits a trailing "(...)" evidence pointer off a claim bullet so it can be
// rendered as a separate muted sub-line instead of sitting mid-sentence.
function splitEvidence(text) {{
  const m = text.match(/^(.*\\S)\\s*\\(([^()]+)\\)\\s*$/);
  if (!m) return {{claim: text, evidence: null}};
  return {{claim: m[1], evidence: m[2]}};
}}

function renderClaimBullet(li, line) {{
  let rest = line;
  let tag = null;
  for (const prefix of Object.keys(CLAIM_TAGS)) {{
    if (rest.startsWith(prefix)) {{
      tag = CLAIM_TAGS[prefix];
      rest = rest.slice(prefix.length).trim();
      break;
    }}
  }}
  const {{claim, evidence}} = splitEvidence(rest);
  if (tag) {{
    li.appendChild(el("span", {{className: "claim-tag " + tag.className, text: tag.label}}));
  }}
  const claimSpan = document.createElement("span");
  claimSpan.innerHTML = inlineFormat(claim);
  li.appendChild(claimSpan);
  if (evidence) {{
    li.appendChild(el("span", {{className: "claim-evidence", text: evidence}}));
  }}
}}

function renderField(container, label, value) {{
  const f = el("div", {{className: "field"}});
  f.appendChild(el("span", {{className: "k", text: label}}));
  const lines = value.split("\\n").map(l => l.trim()).filter(l => l.length > 0);
  const isList = lines.length > 1 && lines.some(l => l.startsWith("- ") || l.startsWith("* "));
  if (isList) {{
    const ul = el("ul", {{className: "field-list"}});
    for (const line of lines) {{
      const li = document.createElement("li");
      const isBullet = line.startsWith("- ") || line.startsWith("* ");
      if (isBullet) {{
        renderClaimBullet(li, line.slice(2).trim());
      }} else {{
        li.innerHTML = inlineFormat(line);
      }}
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
  const card = el("div", {{className: "card " + badgeClass(s)}});

  const head = el("div", {{className: "card-head"}});
  head.appendChild(el("h3", {{className: "card-title", text: s.title}}));
  head.appendChild(el("span", {{className: "badge " + badgeClass(s), text: badgeLabel(s)}}));
  card.appendChild(head);

  const meta = el("div", {{className: "card-meta"}});
  if (opts.showProject) {{
    const chip = el("span", {{className: "project-chip", text: s.project}});
    chip.style.setProperty("--chip-hue", String(projectHue(s.project)));
    meta.appendChild(chip);
  }}
  const when = el("span", {{text: s.last_activity_valid ? formatFriendlyDate(s.ended_at) : "Date unavailable"}});
  if (s.last_activity_valid) when.title = s.ended_at;
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

  if (s.live_snapshot) {{
    const snap = s.live_snapshot;
    const live = el("div", {{className: "card-live"}});
    if (snap.prompt) live.appendChild(el("div", {{text: `Now: "${{snap.prompt}}"`}}));
    if (snap.latest) live.appendChild(el("div", {{text: `Latest: ${{snap.latest}}`}}));
    const toolText = Object.entries(snap.tools || {{}}).map(([k, v]) => `${{k}}×${{v}}`).join(", ");
    if (toolText) live.appendChild(el("div", {{text: `Recent tools: ${{toolText}}`}}));
    if (snap.files && snap.files.length) live.appendChild(el("div", {{text: `Files: ${{snap.files.join(", ")}}`}}));
    card.appendChild(live);
  }}

  if (s.summary_behind_bytes) {{
    const kb = Math.max(1, Math.round(s.summary_behind_bytes / 1024));
    card.appendChild(el("div", {{className: "card-pending",
      text: `Summary is behind: ${{kb}} KB of activity since it was written.`}}));
  }}

  if (s.last_prompt && !s.live_snapshot) {{
    card.appendChild(el("div", {{className: "card-last-prompt",
      text: `Last prompt: "${{s.last_prompt}}"`}}));
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
  }} else {{
    const disabledBtn = el("button", {{className: "btn btn-primary", text: "Copy resume command"}});
    disabledBtn.disabled = true;
    disabledBtn.title = s.resume_unavailable_reason || "unavailable";
    actions.appendChild(disabledBtn);
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
    const reason = s.resume_unavailable_reason || "unavailable";
    details.appendChild(el("div", {{className: "no-resume",
      text: `Resume command unavailable — ${{reason}}.`}}));
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

function renderHeaderStats() {{
  const projectCount = getProjectGroups().size;
  const sessionCount = SESSIONS.length;
  const projectWord = projectCount === 1 ? "project" : "projects";
  const sessionWord = sessionCount === 1 ? "session" : "sessions";
  const stats = document.getElementById("header-stats");
  stats.title = "Updated " + GENERATED_AT;
  stats.innerHTML = "";
  const part = (n, word) => {{
    const span = document.createElement("span");
    const b = document.createElement("strong");
    b.textContent = String(n);
    span.appendChild(b);
    span.appendChild(document.createTextNode(" " + word));
    return span;
  }};
  stats.appendChild(part(projectCount, projectWord));
  stats.appendChild(el("span", {{className: "slash", text: "/"}}));
  stats.appendChild(part(sessionCount, sessionWord));
}}

function formatShortDateTime(iso) {{
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "";
  const date = d.toLocaleDateString(undefined, {{day: "numeric", month: "short"}});
  const time = d.toLocaleTimeString(undefined, {{hour: "2-digit", minute: "2-digit", hour12: false}});
  return `${{date}} · ${{time}}`;
}}

function formatShortDate(iso) {{
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "";
  return d.toLocaleDateString(undefined, {{day: "numeric", month: "short"}});
}}

function matchesSearch(s) {{
  if (!searchQuery) return true;
  const haystack = (s.project + " " + (s.cwd || "") + " " + s.title + " " + s.summary + " " +
    s.worked_on + " " + (s.next_action_required || "")).toLowerCase();
  return haystack.includes(searchQuery);
}}

function renderClearFilters() {{
  const btn = document.getElementById("clearFilters");
  btn.hidden = !searchQuery;
}}

function latestValidEndedAt(sessions) {{
  // Sessions within a group are already ordered newest-first by ended_at
  // (getProjectGroups), and the epoch fallback ("1970-01-01T00:00:00Z")
  // always string-sorts behind any real timestamp, so the first VALID
  // session in that order is also the most recent valid one.
  const valid = sessions.find(s => s.last_activity_valid);
  return valid ? valid.ended_at : null;
}}

function projectNextAction(sessions) {{
  // sessions is already sorted newest-first; only real required-action text
  // is ever considered here — optional suggestions must never be promoted.
  const withRequired = sessions.filter(s => s.next_action_required);
  if (withRequired.length === 0) {{
    return {{ kind: "none", text: "No required next action" }};
  }}
  const distinct = new Set(withRequired.map(s => s.next_action_required.trim()));
  if (distinct.size > 1) {{
    return {{ kind: "multiple", text: "Multiple open threads — see sessions below." }};
  }}
  // All sessions that have a required action agree (or there's only one) —
  // surface the most recent one, even if a newer session in the group has
  // no required action of its own.
  return {{ kind: "single", text: withRequired[0].next_action_required }};
}}

function renderProjectGrid(filtered) {{
  const grid = document.getElementById("project-grid");
  grid.innerHTML = "";

  const matchedProjects = new Set(filtered.map(s => s.project));
  const groups = getProjectGroups();
  const projectNames = [...groups.keys()]
    .filter(p => matchedProjects.has(p))
    .sort((a, b) => {{
      const keyA = latestValidEndedAt(groups.get(a));
      const keyB = latestValidEndedAt(groups.get(b));
      // Projects with no genuinely-dated session ever sort after every
      // project that has one — never intermixed, never treated as "most
      // recent" just because a fallback string happened to compare a
      // certain way.
      if (keyA === null && keyB === null) return 0;
      if (keyA === null) return 1;
      if (keyB === null) return -1;
      return keyB.localeCompare(keyA);
    }});

  if (projectNames.length === 0) return 0;

  for (const project of projectNames) {{
    const sessions = groups.get(project);
    const card = el("button", {{className: "project-card"}});
    card.type = "button";
    card.style.setProperty("--chip-hue", String(projectHue(project)));

    const intro = el("div", {{className: "project-intro"}});
    const top = el("div", {{className: "project-top"}});
    top.appendChild(el("span", {{className: "project-icon", text: (project.match(/[A-Za-z0-9]/) || ["?"])[0]}}));
    const head = el("div", {{className: "project-card-head"}});
    head.appendChild(el("div", {{className: "project-name", text: project}}));
    const latestValid = latestValidEndedAt(sessions);
    const longRunning = sessions.find(s => s.long_running_open);
    head.appendChild(el("div", {{className: "project-card-activity",
      text: longRunning ? longRunning.long_running_label :
        (latestValid ? `Last session ${{formatShortDateTime(latestValid)}}` : "Date unavailable")}}));
    top.appendChild(head);
    intro.appendChild(top);
    intro.appendChild(el("div", {{className: "project-path", text: sessions[0].cwd_short}}));

    const recap = PROJECT_RECAPS[project];
    if (recap) {{
      intro.appendChild(el("div", {{className: "project-recap", text: recap}}));
    }}
    card.appendChild(intro);

    const nextInfo = projectNextAction(sessions);
    const nextLine = el("div", {{className: "project-card-next" + (nextInfo.kind === "none" ? " no-action" : "")}});
    nextLine.appendChild(el("span", {{className: "k", text: "Next"}}));
    nextLine.appendChild(document.createTextNode(nextInfo.text));
    card.appendChild(nextLine);

    const shown = sessions.slice(0, 4);
    const pad = (n) => String(n).padStart(2, "0");
    const recentHead = el("div", {{className: "recent-header"}});
    recentHead.appendChild(el("span", {{text: "Recent sessions"}}));
    recentHead.appendChild(el("span", {{text: `${{pad(shown.length)}} / ${{pad(sessions.length)}}`}}));
    card.appendChild(recentHead);

    const preview = el("div", {{className: "project-preview"}});
    for (const s of shown) {{
      const row = el("div", {{className: "preview-row"}});
      row.appendChild(el("span", {{className: "preview-dot " + badgeClass(s)}}));
      row.appendChild(el("span", {{className: "preview-title", text: s.title}}));
      row.appendChild(el("span", {{className: "preview-date",
        text: s.long_running_open ? s.long_running_label :
          (s.last_activity_valid ? formatShortDate(s.ended_at) : "Date unavailable")}}));
      preview.appendChild(row);
    }}
    card.appendChild(preview);

    const foot = el("div", {{className: "project-foot"}});
    const viewAll = el("button", {{className: "view-all-row",
      text: `View all ${{sessions.length}} session${{sessions.length === 1 ? "" : "s"}}`}});
    viewAll.type = "button";
    viewAll.addEventListener("click", (e) => {{
      e.stopPropagation();
      location.hash = "project=" + encodeURIComponent(project);
    }});
    foot.appendChild(viewAll);
    card.appendChild(foot);

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

  const filtered = SESSIONS.filter(s => matchesSearch(s));

  document.getElementById("overview-head").hidden = currentView !== "grid";
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

renderHeaderStats();
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
