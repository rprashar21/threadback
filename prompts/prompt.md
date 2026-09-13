Improve my existing Claude Code `/recap` tool into a simple, trustworthy work-continuity dashboard that could be useful to other developers.

The core user question is: “I have been away for a few days. Which projects did I work on, what actually happened, and which session should I resume?”

## Target dashboard structure

/recap home page
└── Projects, ordered by most recent session activity
├── Project name, shortened real path, last-used date
├── Two-line recap across the project's recent sessions
├── Three most recent sessions, visible immediately
│    ├── Short title and date
│    ├── One-sentence account of what happened
│    ├── Next concrete action, or “No required next action”
│    ├── Copyable `cd <project> && claude --resume <session-id>` command
│    └── Expand in place for completed work, decisions and evidence
└── “Show older sessions” within the same project section

Do not add a separate project page or multi-step drill-down. Do not organise the home page around Completed/In Progress/Blocked categories. Those classifications may remain in stored data for accuracy, but the main navigation is PROJECTS.

Provide one search input that filters by project name/path. Put the most recently used project first. Sort sessions newest first within each project. Keep the page self-contained, local, responsive, and visually restrained.

## What makes this useful beyond a session browser

1. Project-level recap

For each project, synthesize a concise recap from its recent sessions. It should answer:

* What was actually accomplished across those sessions?
* What is the most relevant next step, if any?

Do not simply concatenate session summaries. If recent sessions cover unrelated tasks in the same repository, do not pretend they are one task; say so briefly or list the distinct threads.

A project recap should be at most two short sentences. The session entries beneath it provide detail.

2. Facts versus suggestions

Distinguish:

* Verified action: a file was changed, a command was run, tests passed, or an output was produced.
* Discussion or plan: an approach was proposed but not implemented.
* Uncertain claim: the transcript does not establish what happened.

Never write “implemented,” “fixed,” or “verified” based only on Claude saying it intended to do so. Do not treat an optional suggestion as required next work. Show “No required next action” when the requested work was finished.

In expanded session details, make important claims traceable to the source session. Use compact evidence such as a relevant file path, test result, or transcript reference where available. Do not dump the whole transcript into the HTML.

3. Useful on first run

The dashboard must discover existing Claude Code transcripts even for sessions that predate this tool's hooks. A new user should get a project list and resumable sessions on the first `/recap` run.

If an old session has no reliable summary, show “Summary not available” and offer an explicit way to summarize it. Never invent a status, project path, or next action.

4. Recovery after interruptions

If a SessionEnd hook fails, a session crashes, or a recap is stale, the session should still be discoverable from its transcript and have a resume command when the ID and project path are reliable.

Show when a summary was generated and whether it came from the end hook or a later `/recap` recovery pass. Label old or incomplete summaries honestly. A recent transcript timestamp is activity evidence, not proof of task completion.

5. Privacy and control

Keep the dashboard and session metadata local. Explain clearly whether the optional summarization step sends transcript content to the configured Claude model; do not describe the entire process as offline if it is not.

Add a simple way to exclude project paths from indexing or summarization. Do not include raw secrets or full transcripts in the generated HTML. Escape transcript-derived text. Do not collect telemetry or publish the dashboard.

## Context usage

I am also interested in context usage, but it is secondary to the work recap. Inspect actual transcript usage fields first. Show a small, accurately labelled metric only if you can compute it reliably. Peak context-window usage and total session tokens are different metrics; never label one as the other. If unavailable, omit the metric rather than estimate it.

Do not add cost charts, token dashboards, tool-call timelines, sidebars, or other forensic features.

## Implementation approach

First inspect the existing hooks, session records, transcript reconciliation, generator and `/recap` skill. Preserve working parts and avoid a rewrite.

Before modifying code, report:

* Which requested features already exist
* Which require data or summarization changes
* Which are only presentation changes
* Any unverified assumptions
* The smallest file-change plan

Then show a mockup of one project containing an unfinished session and a completed session. Wait for my confirmation of the layout before implementing.

After approval, implement in small steps:

1. Make first-run transcript discovery and project mapping reliable.
2. Ensure session summaries separate verified actions, discussion and uncertainty.
3. Produce the short project-level recap.
4. Render the one-page, project-first layout.
5. Add project-name filtering, older-session expansion and copy-resume controls.
6. Add privacy exclusions and a clear explanation of summarization behaviour.
7. Add context usage only if a trustworthy metric is available.

Test with:

* A project with one session
* A project with more than five sessions
* Multiple unrelated tasks in one project
* Old transcript with no summary
* Interrupted session with no end-hook summary
* Duplicate records for the same session ID
* Discussed-but-not-implemented work
* Completed work with only optional follow-ups
* Paths containing spaces
* An excluded project
* Missing token-usage data

Show the generated dashboard with sample or safely redacted data and explain why each example summary is trustworthy. Do not modify unrelated project repositories.
