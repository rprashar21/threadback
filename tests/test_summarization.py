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
            "Recent work: Added content-hash caching. Earlier: Investigated transcript size limits. "
            "Next: Run the full verification suite.",
        )

    def test_strips_evidence_tags_and_keeps_three_subjects_newest_first(self):
        summaries = [
            {"completed": "[Verified] Wrote the skill files.", "worked_on": "", "next_action": ""},
            {"completed": "[Uncertain] Ported the dark UI.", "worked_on": "", "next_action": ""},
            {"completed": "[Discussed] [Verified] Generated the dashboard.", "worked_on": "", "next_action": ""},
            {"completed": "Fourth item is dropped.", "worked_on": "", "next_action": ""},
        ]

        recap = dashboard.deterministic_project_recap(summaries)

        self.assertEqual(
            recap,
            "Recent work: Wrote the skill files. Earlier: Ported the dark UI; Generated the dashboard.",
        )
        self.assertNotIn("[", recap)

    def test_strips_tags_behind_a_bullet_marker(self):
        recap = dashboard.deterministic_project_recap([
            {"completed": "- [Verified] Wrote the skill files.\n- [Verified] Ran the tests.",
             "worked_on": "", "next_action": "- [Verified] Answer the quiz."},
        ])

        self.assertEqual(recap, "Recent work: Wrote the skill files. Next: Answer the quiz.")

    def test_long_fragment_is_cut_at_a_word_boundary(self):
        long_sentence = "word " * 100
        recap = dashboard.deterministic_project_recap(
            [{"completed": long_sentence, "worked_on": "", "next_action": ""}]
        )

        self.assertTrue(recap.endswith("…."), recap[-10:])
        self.assertLessEqual(len(recap), 260)
        self.assertNotIn("wor…", recap)


class ProjectNarrativeTests(unittest.TestCase):
    SUMMARIES = [
        {"status": "Completed", "worked_on": "Built the dark UI.", "completed": "Ported tokens.",
         "next_action": "None."},
        {"status": "In Progress", "worked_on": "Wrote a teaching skill.", "completed": "Nothing notable.",
         "next_action": "Run a practice round."},
    ]

    def test_disabled_by_default_makes_no_model_call(self):
        with mock.patch.dict("os.environ", {}, clear=False), \
             mock.patch.object(dashboard.ss, "run_project_narrative") as run:
            dashboard.os.environ.pop("RECAP_PROJECT_SUMMARY", None)
            self.assertFalse(dashboard.project_narrative_enabled())
            run.assert_not_called()

    def test_digest_is_oldest_first_and_bounded(self):
        digest = dashboard.build_project_digest(self.SUMMARIES)

        self.assertLess(digest.index("Wrote a teaching skill"), digest.index("Built the dark UI"))
        self.assertLessEqual(len(digest), dashboard.PROJECT_DIGEST_MAX_CHARS)

    def test_cache_hit_skips_model_and_changed_input_regenerates(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "project_recap.json"
            calls = []

            def fake(project, digest, **_):
                calls.append(digest)
                return "Narrative text."

            with mock.patch.object(dashboard.ss, "run_project_narrative", side_effect=fake):
                budget = {"left": 2}
                first = dashboard.project_narrative(cache, "proj", self.SUMMARIES, budget)
                second = dashboard.project_narrative(cache, "proj", self.SUMMARIES, budget)
                changed = dashboard.project_narrative(
                    cache, "proj", self.SUMMARIES + [{"worked_on": "New thing.", "completed": ""}], budget)

            self.assertEqual((first, second, changed), ("Narrative text.",) * 3)
            self.assertEqual(len(calls), 2)
            self.assertEqual(budget["left"], 0)

    def test_cached_narrative_is_used_without_the_flag_but_never_generated(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "project_recap.json"
            with mock.patch.object(dashboard.ss, "run_project_narrative", return_value="Cached text.") as run:
                dashboard.project_narrative(cache, "proj", self.SUMMARIES, {"left": 1})
                run.reset_mock()
                hit = dashboard.project_narrative(cache, "proj", self.SUMMARIES, {"left": 1}, generate=False)
                miss = dashboard.project_narrative(
                    cache, "proj", self.SUMMARIES + [{"worked_on": "New.", "completed": ""}],
                    {"left": 1}, generate=False)
            self.assertEqual(hit, "Cached text.")
            self.assertIsNone(miss)
            run.assert_not_called()

    def test_session_titles_and_summaries_drop_evidence_tags(self):
        summary = dashboard.make_summary("- [Verified] Created the skill files.", "")
        self.assertEqual(summary, "Created the skill files.")
        self.assertFalse(dashboard.make_title("", "- [Uncertain] Ran the tests.", "proj").startswith("["))

    def test_budget_exhausted_returns_none_without_calling_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(dashboard.ss, "run_project_narrative") as run:
                result = dashboard.project_narrative(
                    Path(tmp) / "project_recap.json", "proj", self.SUMMARIES, {"left": 0})
            self.assertIsNone(result)
            run.assert_not_called()

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


class ProjectOptOutTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.project = Path(self._tmp.name) / "proj"
        (self.project / ".claude").mkdir(parents=True)
        dashboard._OPT_OUT_CACHE.clear()
        self.addCleanup(dashboard._OPT_OUT_CACHE.clear)

    def _write_flag(self, text: str):
        (self.project / ".claude" / "recap.json").write_text(text)

    def test_flag_true_opts_out_and_removal_restores(self):
        self.assertFalse(dashboard.sr.project_opted_out(str(self.project)))
        self._write_flag('{"exclude": true}')
        self.assertTrue(dashboard.sr.project_opted_out(str(self.project)))
        self._write_flag('{"exclude": false}')
        self.assertFalse(dashboard.sr.project_opted_out(str(self.project)))

    def test_invalid_or_non_boolean_flag_does_not_opt_out(self):
        for text in ("not json", '{"exclude": "yes"}', "[]"):
            self._write_flag(text)
            self.assertFalse(dashboard.sr.project_opted_out(str(self.project)))

    def test_dashboard_hides_slug_whose_record_cwd_opted_out(self):
        self._write_flag('{"exclude": true}')
        slug = dashboard.sr.slugify_cwd(str(self.project))
        log_root = Path(self._tmp.name) / "logs"
        (log_root / slug).mkdir(parents=True)
        (log_root / slug / "abc.json").write_text(
            json.dumps({"start": {"cwd": str(self.project)}})
        )
        with mock.patch.object(dashboard, "LOG_ROOT", log_root):
            self.assertTrue(dashboard._is_excluded_slug(slug))
            dashboard._OPT_OUT_CACHE.clear()
            self._write_flag('{"exclude": false}')
            self.assertFalse(dashboard._is_excluded_slug(slug))


class LiveSnapshotTests(unittest.TestCase):
    def _transcript(self, records, pad=0):
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False)
        with tmp:
            if pad:
                tmp.write(json.dumps({"type": "system", "pad": "x" * pad}) + "\n")
            for record in records:
                tmp.write(json.dumps(record) + "\n")
        self.addCleanup(Path(tmp.name).unlink, missing_ok=True)
        return Path(tmp.name)

    def test_extracts_prompt_latest_tools_and_files(self):
        path = self._transcript([
            {"type": "user", "turnOrigin": "human", "message": {"content": "Fix the cache"}},
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "Looking at it now."},
                {"type": "tool_use", "name": "Edit", "input": {"file_path": "/a/b/cache.py"}},
                {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
            ]}},
        ])
        snap = dashboard.scan_live_snapshot(path)
        self.assertEqual(snap["prompt"], "Fix the cache")
        self.assertEqual(snap["latest"], "Looking at it now.")
        self.assertEqual(snap["tools"], {"Edit": 1, "Bash": 1})
        self.assertEqual(snap["files"], ["cache.py"])

    def test_only_reads_the_tail(self):
        path = self._transcript(
            [{"type": "user", "turnOrigin": "human", "message": {"content": "recent"}}],
            pad=5_000,
        )
        snap = dashboard.scan_live_snapshot(path, tail_bytes=1_000)
        self.assertEqual(snap["prompt"], "recent")

    def test_empty_tail_returns_none(self):
        self.assertIsNone(dashboard.scan_live_snapshot(self._transcript([])))


class LiveGateTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict("os.environ", {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sr = dashboard.sr

    def test_requires_growth_since_summary(self):
        record = {"summary": {"summarized_through_bytes": 100_000}}
        self.assertFalse(self.sr.live_refresh_due(record, 100_000 + 1024, 1e9))
        self.assertTrue(self.sr.live_refresh_due(record, 100_000 + 61 * 1024, 1e9))

    def test_interval_and_attempt_cap(self):
        base = {"summary": {"summarized_through_bytes": 0}}
        big = 200 * 1024
        recent = dict(base, live={"attempts": 1, "attempted_epoch": 1e9 - 60})
        self.assertFalse(self.sr.live_refresh_due(recent, big, 1e9))
        old = dict(base, live={"attempts": 1, "attempted_epoch": 1e9 - 3600})
        self.assertTrue(self.sr.live_refresh_due(old, big, 1e9))
        capped = dict(base, live={"attempts": 6, "attempted_epoch": 0})
        self.assertFalse(self.sr.live_refresh_due(capped, big, 1e9))

    def test_lock_blocks_until_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / ".sid.live-lock"
            self.assertTrue(self.sr._acquire_live_lock(lock, 1e12))
            now = lock.stat().st_mtime
            self.assertFalse(self.sr._acquire_live_lock(lock, now + 10))
            self.assertTrue(self.sr._acquire_live_lock(lock, now + 10_000))

    def test_tick_is_checkpoint_only_when_disabled_and_spawns_when_due(self):
        sr = self.sr
        cp = {"checked_at": "t", "transcript_bytes": 500_000, "transcript_mtime": "t"}
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(sr, "LOG_ROOT", Path(tmp)), \
                mock.patch("subprocess.Popen") as popen:
            (Path(tmp) / "slug").mkdir()
            with mock.patch.dict("os.environ", {"RECAP_LIVE_SUMMARY": "0"}):
                self.assertEqual(
                    sr.live_tick("slug", "sid1", tmp, "/t.jsonl", cp, "2026-01-01T00:00:00Z"),
                    "checkpoint-only")
            popen.assert_not_called()
            with mock.patch.dict("os.environ", {"RECAP_LIVE_SUMMARY": "1"}):
                self.assertEqual(
                    sr.live_tick("slug", "sid1", tmp, "/t.jsonl", cp, "2026-01-01T00:00:01Z"),
                    "spawned")
                popen.assert_called_once()
                self.assertEqual(
                    sr.live_tick("slug", "sid1", tmp, "/t.jsonl", cp, "2026-01-01T00:00:02Z"),
                    "not-due")
