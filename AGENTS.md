# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## What this is

A local, cross-project work recap for Codex. Hooks record what happens in every Codex session (start, activity checkpoints, an evidence-based end-of-session summary), and `session_dashboard.py` renders it all into one static HTML dashboard: what's done, what's in progress or blocked, and a one-command way to resume any session. All data stays on this machine, but summaries and project recaps are produced by a real local `Codex -p` subprocess call, which sends that session's transcript to your configured Codex model — the dashboard's own privacy note states this rather than describing the pipeline as fully offline.

To leave a project out of the dashboard (and out of lazy summarization / project-recap generation) entirely, add its path — or a path prefix — to `~/.Codex/session-logs/recap-exclude.txt`, one per line, `#` comments allowed. Absent by default; nothing is excluded until this file exists.

This repo is wired into one machine's `~/.Codex/` via symlinks: `~/.Codex/hooks/*` and `~/.Codex/scripts/session_*.py` point back into this repo. There is no build step, package manifest, or test suite — edit the files here and the symlinked live install picks up the change immediately.

## Running / testing changes

- Regenerate the dashboard by hand: `python3 scripts/session_dashboard.py` (prints the output path, `~/.Codex/session-logs/dashboard.html`).
- Force an immediate (non-lazy) resummarization of one session, bypassing the per-run cap and the "looks live" skip: `python3 scripts/session_dashboard.py --summarize-session <session_id>`.
- There's no automated test suite. Verify hook changes by tailing real records under `~/.Codex/session-logs/<project-slug>/<session_id>.json` and comparing against the transcript at `~/.Codex/projects/<project-slug>/<session_id>.jsonl` after a live session start/stop/end.
- Only stdlib is used in the Python scripts (no dependencies to install).

## Architecture

### The per-session JSON record is the core data model

Every session gets exactly one file: `~/.Codex/session-logs/<project-slug>/<session_id>.json`, holding disjoint sections owned by different writers:

- `start` — written once by `session-start-log.sh` (SessionStart hook).
- `checkpoint` — written/updated by `session-checkpoint.sh` (Stop hook, runs every turn). Deterministic only: transcript size/mtime via `stat`. No LLM call, no transcript parsing — it must stay fast.
- `end` — written synchronously by `session-end-log.sh` (SessionEnd hook): `ended_at`/`reason`, a fact not a status guess.
- `summary` — the only section two different writers can touch: the detached worker spawned by `session-end-log.sh` (`session-summarize-worker.sh` → `session_summarize.py`), and `session_dashboard.py`'s own lazy-summarization backfill pass.

All reads/writes to a record go through `session_record.py::merge_section`, which takes an flock on a sibling `.lock` file so concurrent writers never interleave a read and a write. Two different staleness rules apply depending on section:

- `start`/`checkpoint`/`end`: guarded by wall-clock time — a write applies only if its `event_ts` is >= the section's stored timestamp.
- `summary`: guarded by **content coverage**, not time — a write applies only if its `summarized_through_bytes` is >= what's already stored. This lets a slow summarizer run that read an earlier, smaller transcript snapshot lose to one that covered more content, regardless of which process finishes first.

`project_slug` (from `session_record.slugify_cwd`) mirrors Codex's own project-directory naming convention (`~/.Codex/projects/<slug>/`) exactly, so records and real transcripts can always be matched by a plain directory-name lookup.

### Recursion guard for the summarizer sub-agent

`session_summarize.py::run_bounded_summary` launches `Codex -p` as a detached subprocess to read a transcript and write a markdown summary. That subprocess is itself a full Codex session with its own hooks and its own transcript. Two independent mechanisms prevent it from recursively summarizing itself forever:

1. `CLAUDE_SESSION_LOG_SUMMARIZER=1` is set in its env; every hook script checks this and exits immediately if set.
2. It's always run from a dedicated scratch cwd (`~/.Codex/session-logs/.summarizer-scratch`), so its transcript lands under its own isolated `~/.Codex/projects/<scratch-slug>/` directory, which `session_dashboard.py` explicitly excludes from reconciliation (`SUMMARIZER_SCRATCH_SLUG`).

Both guards matter — the env var stops the hooks from writing a record, but only the scratch-directory exclusion stops `session_dashboard.py`'s reconciliation pass from later finding that sub-session's orphaned transcript and treating it as ordinary unsummarized user work.

### session_dashboard.py combines three sources of truth, keyed by session_id

1. Per-session JSON records (above) — the primary source going forward.
2. Legacy timestamp-named `*.md` files under `~/.Codex/session-logs/<slug>/` from before this JSON pipeline existed — still parsed (`parse_sections`/`LEGACY_SECTION_NAMES`) for history, deduped by `session_id` when present, and preferring a real summary over one still carrying the old placeholder marker (`LEGACY_PLACEHOLDER_MARKER`).
3. Real transcripts under `~/.Codex/projects/<slug>/*.jsonl` — Codex's own ground truth. Any transcript with no record and no legacy entry at all (crashed/killed session, or a hook that never fired) gets a synthesized "orphan" entry via `reconcile_orphans` instead of silently vanishing — but only within `ORPHAN_RECONCILE_MAX_AGE_DAYS` (14 days), so this doesn't retroactively surface months of pre-existing history the first time the pipeline runs on a machine.

On every run, `run_lazy_summarization` also backfills a real summary for up to `LAZY_SUMMARY_CAP` (5) sessions whose summary is missing or stale (`_needs_summary`), skipping anything that still looks actively in use (`_is_live`, activity within `LIVE_THRESHOLD_SECONDS` = 5 minutes). **Status is never inferred from a checkpoint's mere existence** — a session without a real summarizer-produced status always shows as "Unknown," honestly labeled with its last known activity, never guessed as "In Progress" or "Completed."

### Dashboard HTML/JS is generated, not templated from files

`render_html` in `session_dashboard.py` returns one big f-string containing the full page (inline `<style>` and `<script>`, session data serialized as a JSON blob into `SESSIONS`). There's no separate template file or frontend build — to change the dashboard's look or behavior, edit the f-string in `session_dashboard.py` directly. The page has two client-side views (project grid / project detail) navigated via `location.hash`, entirely client-rendered from the embedded `SESSIONS` array — no server, no re-fetching.

The one `innerHTML` usage (`inlineFormat`) escapes user/session-derived text first and only re-inserts a fixed `<code>...</code>` wrapper — not general markdown rendering. Preserve that ordering (escape-then-wrap) if touching it.

### Context usage is transcript-derived, never estimated

`scan_transcript_usage` reads a transcript's own per-turn `usage` blocks (already present in every real Codex transcript) to compute two *distinct* numbers, never conflated: `peak_context_tokens` (the highest `input + cache_creation_input + cache_read_input` seen on any one turn) and `total_output_tokens` (summed `output_tokens` across all turns). No LLM call is involved. Results are cached per session in a `usage` record section, gated by transcript `mtime` the same way `checkpoint` is, so a full transcript rescan only happens when the transcript has actually grown. A transcript with no `usage` blocks at all yields `None` for both fields — the UI omits the metric rather than guessing.

### Project-level recap synthesis

`run_project_recaps` groups sessions by project, takes each project's most recent already-summarized sessions (`PROJECT_RECAP_CANDIDATE_COUNT`, no new transcript read), and makes one bounded `Codex -p` call via `session_summarize.py::run_project_recap` to synthesize an at-most-two-sentence recap — explicitly told to call out unrelated threads rather than pretend a single narrative. Cached in `<slug>/project_recap.json`, regenerated only when the contributing session set/summaries (`based_on` signature) changes, capped per run (`PROJECT_RECAP_CAP`) the same way lazy session summarization is. A project with no real session summaries yet simply has no recap line.
