import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import session_dashboard as dashboard  # noqa: E402
import session_summarize as summarize  # noqa: E402


class EvidenceExtractionTests(unittest.TestCase):
    def _write_transcript(self, records: list[dict]) -> Path:
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False)
        with tmp:
            for record in records:
                tmp.write(json.dumps(record) + "\n")
        self.addCleanup(Path(tmp.name).unlink, missing_ok=True)
        return Path(tmp.name)

    def test_extracts_evidence_and_omits_thinking_and_metadata(self):
        transcript = self._write_transcript([
            {"type": "system", "message": {"content": "hidden metadata"}},
            {"type": "user", "message": {"content": "Please fix the cache."}},
            {"type": "assistant", "message": {"content": [
                {"type": "thinking", "thinking": "private reasoning"},
                {"type": "tool_use", "name": "Bash", "input": {
                    "command": "python3 -m unittest", "description": "Run tests",
                }},
            ]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "content": "OK\n" + ("x" * 5_000)},
            ]}},
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "The cache is fixed and tests pass."},
            ]}},
        ])

        first = summarize.prepare_summary_evidence(str(transcript))
        second = summarize.prepare_summary_evidence(str(transcript))

        self.assertIsNotNone(first)
        self.assertEqual(first.evidence_hash, second.evidence_hash)
        self.assertLessEqual(first.input_chars, summarize.MAX_EVIDENCE_CHARS)
        self.assertIn("Please fix the cache", first.text)
        self.assertIn("python3 -m unittest", first.text)
        self.assertIn("tests pass", first.text)
        self.assertNotIn("private reasoning", first.text)
        self.assertNotIn("hidden metadata", first.text)
        self.assertNotIn("x" * 2_500, first.text)

    def test_whole_document_cap_preserves_head_and_tail(self):
        events = [f"EVENT-{i}-" + (str(i) * 2_000) for i in range(40)]
        bounded = summarize._bounded_event_text(events)

        self.assertLessEqual(len(bounded), summarize.MAX_EVIDENCE_CHARS)
        self.assertIn("EVENT-0", bounded)
        self.assertIn("EVENT-39", bounded)
        self.assertIn("middle events omitted", bounded)

    def test_prompt_requires_tagged_bullet_claims(self):
        prompt = summarize.PROMPT_TEMPLATE
        for tag in ("[Verified]", "[Discussed]", "[Uncertain]"):
            self.assertIn(tag, prompt)
        self.assertIn("one bullet per line", prompt)
        self.assertIn("at most 5 bullets", prompt)

    def test_summary_cache_requires_hash_and_prompt_version(self):
        prepared = summarize.PreparedEvidence("evidence", "abc", 123)
        matching = {"prompt_version": summarize.PROMPT_VERSION, "evidence_hash": "abc"}

        self.assertTrue(summarize.summary_matches_evidence(matching, prepared))
        self.assertFalse(summarize.summary_matches_evidence({**matching, "evidence_hash": "def"}, prepared))
        self.assertFalse(summarize.summary_matches_evidence({"evidence_hash": "abc"}, prepared))

    def test_summary_command_has_hard_spend_ceiling(self):
        with mock.patch.dict("os.environ", {"RECAP_SUMMARY_MAX_BUDGET_USD": "0.07"}):
            command = summarize._claude_summary_command("prompt")

        budget_index = command.index("--max-budget-usd")
        self.assertEqual(command[budget_index + 1], "0.07")

    def test_cache_hit_advances_coverage_without_launching_model(self):
        prepared = summarize.PreparedEvidence("evidence", "abc", 456)
        existing = {
            "prompt_version": summarize.PROMPT_VERSION,
            "evidence_hash": "abc",
            "summarized_through_bytes": 123,
            "summary_at": "2026-01-01T00:00:00Z",
        }
        with (
            mock.patch.object(summarize.sr, "read_record", return_value={"summary": existing}),
            mock.patch.object(summarize.sr, "merge_section", return_value=True) as merge,
            mock.patch.object(summarize.subprocess, "Popen") as popen,
        ):
            result = summarize.run_bounded_summary(
                "/path/does/not/matter.jsonl", "project", "session", "test", prepared=prepared,
            )

        self.assertTrue(result)
        popen.assert_not_called()
        self.assertEqual(merge.call_args.args[3]["summarized_through_bytes"], 456)

    def test_summary_fields_are_hard_capped(self):
        body = "\n".join([
            "## Status", "Completed",
            "## Worked On", "x" * 3_000,
            "## Completed", "Done.",
            "## Stopped At", "Nothing notable.",
            "## Next Action", "No required next action.",
        ])

        parsed = summarize.parse_summary_body(body)

        self.assertEqual(parsed["status"], "Completed")
        self.assertLessEqual(len(parsed["worked_on"]), summarize.MAX_SUMMARY_FIELD_CHARS)


class LastUserPromptTests(unittest.TestCase):
    def _write_transcript(self, records: list[dict]) -> Path:
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False)
        with tmp:
            for record in records:
                tmp.write(json.dumps(record) + "\n")
        self.addCleanup(Path(tmp.name).unlink, missing_ok=True)
        return Path(tmp.name)

    def test_returns_last_real_user_text_not_tool_result_echo(self):
        transcript = self._write_transcript([
            {"type": "user", "turnOrigin": "human",
             "message": {"content": "First question about caching."}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Sure."}]}},
            {"type": "user", "turnOrigin": "human", "message": {"content": [
                {"type": "tool_result", "content": "OK\n" + ("x" * 500)},
            ]}},
            {"type": "user", "turnOrigin": "human",
             "message": {"content": "Now add some colors for better view??"}},
        ])

        result = dashboard._last_user_prompt(transcript)

        self.assertEqual(result, "Now add some colors for better view??")

    def test_clips_long_prompt_to_max_chars(self):
        transcript = self._write_transcript([
            {"type": "user", "turnOrigin": "human", "message": {"content": "y" * 500}},
        ])

        result = dashboard._last_user_prompt(transcript, max_chars=50)

        self.assertLessEqual(len(result), 50)

    def test_returns_none_when_no_user_text_present(self):
        transcript = self._write_transcript([
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}},
        ])

        self.assertIsNone(dashboard._last_user_prompt(transcript))

    def test_skips_harness_injected_turns_not_typed_by_a_human(self):
        # A background-task completion notification and a skill-load block
        # both land in the transcript as ordinary type="user" text records
        # (not tool_result echoes), but neither is something the person
        # typed — turnOrigin is how Claude Code itself distinguishes them.
        transcript = self._write_transcript([
            {"type": "user", "turnOrigin": "human",
             "message": {"content": "Add some colors for better view??"}},
            {"type": "user", "isMeta": True,
             "message": {"content": "Base directory for this skill: ..."}},
            {"type": "user", "turnOrigin": "task_notification",
             "message": {"content": "<task-notification>...finished</task-notification>"}},
        ])

        result = dashboard._last_user_prompt(transcript)

        self.assertEqual(result, "Add some colors for better view??")


class DeterministicProjectRecapTests(unittest.TestCase):
    def test_builds_recap_without_model_call(self):
        summaries = [
            {
                "status": "Completed",
                "completed": "Added content-hash caching.",
                "worked_on": "Caching.",
                "next_action": "No required next action.",
            },
            {
                "status": "In Progress",
                "completed": "Nothing notable.",
                "worked_on": "Investigated transcript size limits.",
                "next_action": "Run the full verification suite.",
            },
        ]

        recap = dashboard.deterministic_project_recap(summaries)

        self.assertEqual(
            recap,
            "Recent work spans: Added content-hash caching; Investigated transcript size limits. "
            "Next: Run the full verification suite.",
        )

    def test_returns_none_without_meaningful_summary(self):
        self.assertIsNone(dashboard.deterministic_project_recap([
            {"completed": "Nothing notable.", "worked_on": "Nothing notable.", "next_action": ""},
        ]))


class DashboardBudgetTests(unittest.TestCase):
    def test_lazy_backfill_enforces_model_call_budget(self):
        combined = {}
        for index in range(4):
            tmp = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False)
            tmp.write(b"{}\n")
            tmp.close()
            path = Path(tmp.name)
            self.addCleanup(path.unlink, missing_ok=True)
            combined[str(index)] = dashboard.Combined(
                session_id=str(index),
                cwd=f"/tmp/project-{index}",
                record={"end": {"ended_at": f"2026-09-1{index}T00:00:00Z"}},
                transcript_path=path,
            )

        def prepare(path: str):
            return summarize.PreparedEvidence("x" * 40_000, Path(path).name, 3)

        with (
            mock.patch.object(dashboard.ss, "prepare_summary_evidence", side_effect=prepare),
            mock.patch.object(dashboard.ss, "run_bounded_summary", return_value=True) as run,
        ):
            dashboard.run_lazy_summarization(combined)

        self.assertEqual(run.call_count, dashboard.MAX_SUMMARY_CALLS_PER_RUN)


class ActiveSessionDisplayTests(unittest.TestCase):
    def test_resumed_session_open_over_two_hours_gets_dashboard_marker(self):
        session = dashboard.Combined(
            session_id="resumed",
            cwd="/tmp/project",
            record={
                "start": {
                    "started_at": "2026-09-19T08:00:00Z",
                    "_ts": "2026-09-19T10:00:00Z",
                },
                "end": {
                    "ended_at": "2026-09-19T09:00:00Z",
                    "_ts": "2026-09-19T09:00:00Z",
                },
            },
        )
        now = datetime(2026, 9, 19, 12, 5, tzinfo=timezone.utc)

        self.assertFalse(dashboard._has_ended(session))
        self.assertEqual(dashboard._long_running_open_label(session, now), "Open for 2h 5m")
        with mock.patch.object(dashboard, "_now", return_value=now):
            rendered = dashboard.build_session_data({"resumed": session})[0]
        self.assertTrue(rendered["long_running_open"])
        self.assertEqual(rendered["long_running_label"], "Open for 2h 5m")

    def test_end_after_latest_start_removes_open_marker(self):
        session = dashboard.Combined(
            session_id="ended",
            cwd="/tmp/project",
            record={
                "start": {"_ts": "2026-09-19T08:00:00Z"},
                "end": {"_ts": "2026-09-19T11:00:00Z"},
            },
        )
        now = datetime(2026, 9, 19, 12, 5, tzinfo=timezone.utc)

        self.assertTrue(dashboard._has_ended(session))
        self.assertIsNone(dashboard._long_running_open_label(session, now))


if __name__ == "__main__":
    unittest.main()
