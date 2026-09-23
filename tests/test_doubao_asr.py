"""Offline synthetic tests; no credentials, media, or paid requests."""
import argparse
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("doubao_asr", Path(__file__).parents[1] / "scripts" / "doubao_asr.py")
asr = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(asr)


def response():
    return {"audio_info": {"duration": 1800}, "result": {"text": "你好。再见。", "utterances": [
        {"start_time": 0, "end_time": 900, "text": "你好。", "words": [
            {"text": "你", "start_time": 0, "end_time": 200, "confidence": 0.9},
            {"text": "好", "start_time": 500, "end_time": 900}]},
        {"start_time": 1500, "end_time": 1800, "text": "再见。", "words": [
            {"text": "再见", "start_time": 1500, "end_time": 1800}]}]}}


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.audio = self.root / "source.wav"
        self.audio.write_bytes(b"synthetic-not-a-media-file")
        self.out = self.root / "out"
        self.args = argparse.Namespace(audio=str(self.audio), outdir=str(self.out), format="wav",
            auth="api-key", prompt=False, hotwords=None, wait_seconds=0, poll_seconds=20, gap_ms=200)
        self.env = patch.dict(os.environ, {"DOUBAO_API_KEY": "synthetic-test-only-key",
            "DOUBAO_AUDIO_URL": "https://example.invalid/audio.wav?signature=synthetic"}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def invoke(self, call):
        with contextlib.redirect_stdout(io.StringIO()):
            return asr.run(self.args, call=call)

    def test_timing_and_gap_classes(self):
        summary = asr.normalize(response(), self.out)
        self.assertEqual(summary, {"utterances": 2, "words": 3, "gap_candidates": 2, "granularity": "word"})
        data = json.loads((self.out / "transcript.json").read_text(encoding="utf-8"))
        self.assertEqual([g["kind"] for g in data["gap_candidates"]], ["within_utterance", "between_utterances"])
        self.assertEqual([g["duration_ms"] for g in data["gap_candidates"]], [300, 600])
        self.assertEqual(data["words"][-1]["text"], "再见")  # One word stays one word, never two invented characters.

    def test_no_words_is_explicit_downgrade(self):
        payload = response()
        for item in payload["result"]["utterances"]:
            item.pop("words")
        summary = asr.normalize(payload, self.out)
        self.assertEqual(summary["granularity"], "utterance")
        self.assertEqual(summary["words"], 0)
        self.assertEqual(summary["gap_candidates"], 0)

    def test_text_only(self):
        summary = asr.normalize({"result": {"text": "只有文字"}}, self.out)
        self.assertEqual(summary["granularity"], "text_only")
        self.assertEqual((self.out / "transcript.txt").read_text(encoding="utf-8"), "只有文字")

    def test_overlap_does_not_create_false_gap(self):
        payload = {"result": {"utterances": [{"words": [
            {"text": "长词", "start_time": 0, "end_time": 1000},
            {"text": "重叠", "start_time": 200, "end_time": 300},
            {"text": "下词", "start_time": 800, "end_time": 1100}]}]}}
        self.assertEqual(asr.normalize(payload, self.out)["gap_candidates"], 0)

    def test_invalid_timestamps_not_fabricated(self):
        payload = {"result": {"utterances": [{"words": [
            {"text": "无", "start_time": "0", "end_time": 100},
            {"text": "反", "start_time": 100, "end_time": 0},
            {"text": "坏", "start_time": float("nan"), "end_time": 300}]}]}}
        asr.normalize(payload, self.out)
        data = json.loads((self.out / "transcript.json").read_text(encoding="utf-8"))
        self.assertEqual(data["invalid_words_skipped"], 3)
        self.assertEqual(data["words"], [])

    def test_request_preserves_disfluencies(self):
        body = asr.request_body("https://example.invalid/a.wav", "wav", ["示例术语"])
        self.assertFalse(body["request"]["enable_ddc"])
        self.assertFalse(body["request"]["enable_itn"])
        self.assertTrue(body["request"]["show_utterances"])
        self.assertEqual(json.loads(body["request"]["corpus"]["context"])["hotwords"], [{"word": "示例术语"}])

    def test_url_validation(self):
        for url in ("file:///tmp/a.wav", "http://example.invalid/a.wav", "https://name:password@example.invalid/a.wav"):
            with self.assertRaises(asr.UserError):
                asr.validate_url(url)

    def test_legacy_auth(self):
        with patch.dict(os.environ, {"DOUBAO_APP_ID": "synthetic-app", "DOUBAO_ACCESS_TOKEN": "synthetic-token"}, clear=True):
            self.assertEqual(asr.auth_headers("legacy"), {"X-Api-App-Key": "synthetic-app", "X-Api-Access-Key": "synthetic-token"})

    def test_submit_complete_and_offline_reuse(self):
        calls = []
        def api(action, auth, task, body):
            calls.append((action, task))
            if action == "submit":
                saved = json.loads((self.out / "task.json").read_text())
                self.assertEqual(saved["task_id"], task)
                self.assertEqual(saved["status"], "submitting")
                return 200, asr.SUCCESS, {}
            payload = response()
            payload["audio_url"] = os.environ["DOUBAO_AUDIO_URL"]
            payload["diagnostic"] = os.environ["DOUBAO_API_KEY"]
            return 200, asr.SUCCESS, payload
        self.assertEqual(self.invoke(api), 0)
        self.assertEqual([x[0] for x in calls], ["submit", "query"])
        self.assertEqual(calls[0][1], calls[1][1])
        for path in self.out.iterdir():
            content = path.read_text(encoding="utf-8-sig")
            self.assertNotIn(os.environ["DOUBAO_API_KEY"], content)
            self.assertNotIn(os.environ["DOUBAO_AUDIO_URL"], content)
            self.assertNotIn(str(self.audio), content)
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(self.invoke(lambda *a: self.fail("completed task performed network")), 0)

    def test_pending_then_resume_without_url(self):
        def pending(action, *unused):
            return 200, asr.SUCCESS if action == "submit" else "20000002", {}
        self.assertEqual(self.invoke(pending), 2)
        with patch.dict(os.environ, {"DOUBAO_API_KEY": "synthetic-test-only-key"}, clear=True):
            def finish(action, *unused):
                self.assertEqual(action, "query")
                return 200, asr.SUCCESS, response()
            self.assertEqual(self.invoke(finish), 0)

    def test_unknown_submit_is_not_resubmitted(self):
        def broken(*unused):
            raise TimeoutError("sensitive URL must not escape")
        with self.assertRaisesRegex(asr.UserError, "Submission outcome unknown"):
            self.invoke(broken)
        def query(action, *unused):
            self.assertEqual(action, "query")
            return 200, asr.SUCCESS, response()
        self.assertEqual(self.invoke(query), 0)

    def test_changed_audio_rejected_before_network(self):
        self.invoke(lambda action, *a: (200, asr.SUCCESS if action == "submit" else "20000002", {}))
        self.audio.write_bytes(b"different")
        with self.assertRaisesRegex(asr.UserError, "differs"):
            self.invoke(lambda *a: self.fail("network"))

    def test_rejected_submit_stops_future_network(self):
        with self.assertRaisesRegex(asr.UserError, "Submit rejected"):
            self.invoke(lambda *a: (403, "45000000", {}))
        with self.assertRaisesRegex(asr.UserError, "Saved task failed"):
            self.invoke(lambda *a: self.fail("network"))

    def test_query_exception_preserves_resumable_task(self):
        def first(action, *unused):
            if action == "submit":
                return 200, asr.SUCCESS, {}
            raise RuntimeError("do not print secrets")
        with self.assertRaisesRegex(asr.UserError, "Query interrupted"):
            self.invoke(first)
        self.assertEqual(self.invoke(lambda action, *a: (200, asr.SUCCESS, response()) if action == "query" else self.fail("resubmitted")), 0)

    def test_redirects_blocked(self):
        self.assertIsNone(asr.NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.invalid"))

    def test_hidden_url_echo_is_not_saved(self):
        self.args.prompt = True
        values = {"DOUBAO_API_KEY": "synthetic-prompt-key", "DOUBAO_AUDIO_URL": "https://example.invalid/hidden?signature=prompt"}
        def api(action, *unused):
            if action == "submit":
                return 200, asr.SUCCESS, {}
            payload = response()
            payload["result"]["text"] = values["DOUBAO_AUDIO_URL"]
            payload["unexpected_echo"] = values["DOUBAO_AUDIO_URL"]
            return 200, asr.SUCCESS, payload
        with patch.dict(os.environ, {}, clear=True), patch.object(asr, "secret", side_effect=lambda name, *a: values[name]):
            self.assertEqual(self.invoke(api), 0)
        for path in self.out.iterdir():
            self.assertNotIn(values["DOUBAO_AUDIO_URL"], path.read_text(encoding="utf-8-sig"))

    def test_query_failure_can_resume_after_auth_correction(self):
        def api(action, *unused):
            return (200, asr.SUCCESS, {}) if action == "submit" else (403, "45000000", {})
        with self.assertRaisesRegex(asr.UserError, "Query failed"):
            self.invoke(api)
        def resume(action, *unused):
            self.assertEqual(action, "query")
            return 200, asr.SUCCESS, response()
        self.assertEqual(self.invoke(resume), 0)

    def test_unknown_metadata_excluded_even_without_known_url(self):
        payload = response()
        payload["additions"] = {"echo": "https://example.invalid/unknown?signature=synthetic"}
        payload["result"]["debug"] = "synthetic-secret-unknown"
        safe = asr.recognition_response(payload)
        self.assertEqual(set(safe), {"audio_info", "result"})
        self.assertEqual(set(safe["result"]), {"text", "utterances"})

    def test_formula_text_csv_is_escaped(self):
        payload = response()
        payload["result"]["utterances"][0]["words"][0]["text"] = "=1+1"
        asr.normalize(payload, self.out)
        self.assertIn("'=1+1", (self.out / "gap-candidates.csv").read_text(encoding="utf-8-sig"))


if __name__ == "__main__":
    unittest.main()
