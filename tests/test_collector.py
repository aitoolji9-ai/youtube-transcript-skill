import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from caption_collector.formats import CaptionError, Capture, digest, parse, parsing_audit, timed_text, video_id
from caption_collector.providers import api_capture, classify, collect, import_capture, select_track
from caption_collector.cli import handoff, parser, run
from caption_collector.storage import cached, locked, publish, safe_name, staged_capture, verify_record

VID = "AbCdEfGhI12"


def sample(text="같은 말\n같은 말\n끝."):
    raw = json.dumps({"events": [{"tStartMs": 1000 * i, "dDurationMs": 1000, "segs": [{"utf8": t}]} for i, t in enumerate(text.split("\n"))]}, ensure_ascii=False).encode()
    return Capture(VID, "fixture", raw, "json3", parse(raw, "json3"), "ko", True, "보존 시험")


class InputTests(unittest.TestCase):
    def test_all_video_url_shapes(self):
        for url in [VID, f"https://youtu.be/{VID}?si=abc", f"https://www.youtube.com/watch?v={VID}&list=LIST&t=30", f"https://m.youtube.com/shorts/{VID}", f"https://youtube.com/live/{VID}", f"https://www.youtube-nocookie.com/embed/{VID}", f"https://music.youtube.com/watch?v={VID}"]:
            with self.subTest(url=url): self.assertEqual(video_id(url), VID)

    def test_rejects_non_youtube_and_playlist(self):
        for url in [f"https://youtube.com.evil.test/watch?v={VID}", f"https://user:pass{chr(64)}youtube.com/watch?v={VID}", "file:///etc/passwd", "https://youtube.com/playlist?list=abc", f"https://youtu.be/{VID}/junk", f"https://youtube.com/watch?v={VID}&v=11111111111"]:
            with self.subTest(url=url), self.assertRaises(CaptionError): video_id(url)

    def test_json3_preserves_whitespace_repetitions_and_unicode(self):
        raw = '{"events":[{"tStartMs":0,"dDurationMs":1500,"segs":[{"utf8":"  그대로 "},{"utf8":"\\n다시  "}]},{"tStartMs":1000,"segs":[{"utf8":"다시"}]}]}'.encode()
        self.assertEqual(parse(raw, "json3"), [{"text": "  그대로 \n다시  ", "start": 0, "duration": 1.5}, {"text": "다시", "start": 1, "duration": None}])

    def test_json3_display_controls_are_not_caption_cues(self):
        raw = json.dumps({"events": [
            {"tStartMs": 0, "id": 1},
            {"tStartMs": 240, "segs": [{"utf8": " 같은 말\n안녕 "}]},
            {"tStartMs": 1990, "aAppend": 1, "segs": [{"utf8": "\n"}]},
            {"tStartMs": 2000, "segs": [{"utf8": " 같은 말\n안녕 "}]}]}, ensure_ascii=False).encode()
        self.assertEqual(len(parse(raw, "json3", version=1)), 3)
        rows = parse(raw, "json3")
        self.assertEqual([r["text"] for r in rows], [" 같은 말\n안녕 ", " 같은 말\n안녕 "])
        self.assertEqual([r["start"] for r in rows], [.24, 2.0])
        self.assertEqual(parsing_audit(raw, "json3"), {"source_event_count": 4, "text_event_count": 2,
                         "whitespace_control_event_count": 1, "non_text_event_count": 1,
                         "source_text_event_indices": [1, 3]})

    def test_json3_cannot_silently_drop_unknown_text_or_invent_time(self):
        for event in [{"segs": [{"utf8": "time is missing"}]},
                      {"tStartMs": 0, "segs": [{"utf8": "known"}, {"unknown": "lost"}]}]:
            with self.assertRaises(CaptionError): parse(json.dumps({"events": [event]}).encode(), "json3")

    def test_timed_text_keeps_milliseconds_and_unknown_time(self):
        rows = [{"text": "두 줄\n그대로", "start": 3661.24, "duration": 1},
                {"text": "시각 없음", "start": None, "duration": None}]
        self.assertEqual(timed_text(rows), "[01:01:01.240]\n두 줄\n그대로\n\n[시각 미제공]\n시각 없음")

    def test_srt_and_vtt_preserve_cue_text(self):
        for fmt, header, decimal in [("vtt", "WEBVTT\n\n", "."), ("srt", "", ",")]:
            raw = (header + f"1\n00:00:01{decimal}000 --> 00:00:02{decimal}500\n<i>안녕</i>\n같은 말\n\n2\n00:00:02{decimal}000 --> 00:00:03{decimal}000\n같은 말\n").encode()
            rows = parse(raw, fmt)
            self.assertEqual([r["text"] for r in rows], ["<i>안녕</i>\n같은 말", "같은 말"])
            self.assertEqual(rows[0]["duration"], 1.5)

    def test_malformed_cue_is_not_silently_dropped(self):
        for raw in [b"WEBVTT\n\n00:00:00.000 --> BAD\ntext\n", b"1\n00:00:05,000 --> 00:00:03,000\ntext\n"]:
            with self.assertRaises(CaptionError): parse(raw, "vtt")

    def test_empty_and_nonfinite_rejected(self):
        for raw in [b'{"events":[]}', b'[{"text":"hello","start":NaN,"duration":1}]']:
            with self.assertRaises(CaptionError): parse(raw, "json")

    def test_long_transcript_not_truncated(self):
        c = sample("\n".join(f"원문 {i}" for i in range(12000)))
        self.assertEqual(len(c.segments), 12000)
        self.assertTrue(c.text.endswith("원문 11999"))

    def test_reject_wrong_video_and_unknown_browser_header(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "export.txt"
            path.write_text("YouTube transcript\nVideo ID: 11111111111\nLanguage: ko\nCaptions: authored or unspecified\n\n[0:01] 안녕\n")
            with self.assertRaises(CaptionError) as e: import_capture(VID, path, browser_export=True)
            self.assertEqual(e.exception.kind, "wrong_video")

    def test_browser_unknown_provenance_stays_unknown(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "export.txt"
            path.write_text(f"YouTube transcript\nVideo ID: {VID}\nLanguage: ko\nCaptions: authored or unspecified\n\n[0:01] 안녕\n[1:02] 또 안녕\n")
            c = import_capture(VID, path, browser_export=True)
            self.assertIsNone(c.is_generated)
            self.assertEqual(c.text, "안녕\n또 안녕")
            self.assertEqual(c.segments[-1]["start"], 62)
            self.assertEqual(c.raw, path.read_bytes())

    def test_browser_generic_language_accepts_region(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "export.txt"
            path.write_text(f"YouTube transcript\nVideo ID: {VID}\nLanguage: en-US\nCaptions: authored or unspecified\n\n[0:01] hello\n")
            self.assertEqual(import_capture(VID, path, "en", True).language, "en-US")
            with self.assertRaises(CaptionError): import_capture(VID, path, "en-GB", True)

    def test_import_requires_language_and_preserves_plain_text(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "input.txt"; path.write_text("  원문\n\n원문  \n")
            with self.assertRaises(CaptionError): import_capture(VID, path)
            c = import_capture(VID, path, "ko")
            self.assertEqual(c.text, path.read_text())


class RouteTests(unittest.TestCase):
    def test_prefers_manual_of_original_language_not_english_default(self):
        tracks = [{"language": "en", "is_generated": False}, {"language": "ko", "is_generated": True, "original": True}, {"language": "ko", "is_generated": False}]
        t, evidence = select_track(tracks)
        self.assertEqual((t["language"], t["is_generated"]), ("ko", False))
        self.assertEqual(evidence, "provider_original_track")

    def test_ambiguous_language_requires_selection(self):
        with self.assertRaises(CaptionError) as e: select_track([{"language": "en"}, {"language": "ko"}])
        self.assertEqual(e.exception.kind, "needs_language")

    def test_api_generated_type_is_not_original_language_proof(self):
        from types import SimpleNamespace
        with patch("youtube_transcript_api.YouTubeTranscriptApi") as api:
            api.return_value.list.return_value = [
                SimpleNamespace(language_code="en", language="English", is_generated=True),
                SimpleNamespace(language_code="ko", language="Korean", is_generated=False)]
            with self.assertRaises(CaptionError) as error: api_capture(VID)
            self.assertEqual(error.exception.kind, "needs_language")

    def test_regional_manual_caption_precedes_generic_auto(self):
        tracks = [{"language": "en", "is_generated": True, "original": True}, {"language": "en-US", "is_generated": False}]
        for requested in (None, "en", "en-US"):
            track, _ = select_track(tracks, requested)
            self.assertEqual((track["language"], track["is_generated"]), ("en-US", False))
        with self.assertRaises(CaptionError): select_track(tracks, "en-GB")

    def test_requested_language_does_not_fallback(self):
        with self.assertRaises(CaptionError): select_track([{"language": "en"}], "ko")

    def test_first_extractor_fails_second_succeeds(self):
        def first(*args): raise CaptionError("extractor_error", "fixture")
        c, attempts, result = collect(VID, runners={"yt_dlp": first, "youtube_transcript_api": lambda *a: sample()})
        self.assertIsNone(result); self.assertEqual(c.text, sample().text)
        self.assertEqual([a["outcome"] for a in attempts], ["extractor_error", "captured"])

    def test_no_tracks_from_one_method_is_not_no_captions(self):
        def fail(*a): raise CaptionError("no_tracks_reported", "fixture")
        c, attempts, result = collect(VID, runners={"yt_dlp": fail, "youtube_transcript_api": fail})
        self.assertIsNone(c); self.assertEqual(result["status"], "needs_browser")
        self.assertEqual(len(attempts), 2)

    def test_rate_limit_and_access_denial_stop_automatic_hopping(self):
        for kind in ["rate_limited", "request_blocked", "access_denied", "needs_login"]:
            called = []
            def fail(*a): raise CaptionError(kind, "fixture")
            def second(*a): called.append(True); return sample()
            c, attempts, result = collect(VID, runners={"yt_dlp": fail, "youtube_transcript_api": second})
            self.assertEqual(called, []); self.assertIsNone(c); self.assertEqual(result["reason"], kind)

    def test_rate_limit_preserves_server_retry_after(self):
        error = RuntimeError("429 Too Many Requests")
        error.headers = {"Retry-After": "3600"}
        classified = classify(error)
        self.assertEqual(classified.details["retry_after_seconds"], 3600)
        self.assertEqual(classified.details["retry_after_basis"], "server_retry_after")

    def test_transient_retry_is_bounded(self):
        calls = []
        def fail(*a): calls.append(1); raise CaptionError("transient", "fixture")
        with patch("caption_collector.providers.time.sleep"):
            _, attempts, result = collect(VID, provider="yt_dlp", runners={"yt_dlp": fail})
        self.assertEqual(len(calls), 2); self.assertEqual(result["status"], "needs_browser")

    def test_login_only_uses_supported_provider(self):
        seen = []
        def auth(v, l, browser, profile): seen.append((browser, profile)); return sample()
        collect(VID, browser="chrome", profile="Profile 1", runners={"yt_dlp": auth})
        self.assertEqual(seen, [("chrome", "Profile 1")])


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name).resolve()
        self.state = self.root / "state"; self.out = self.root / "out"
    def tearDown(self): self.temp.cleanup()

    def test_full_readback_and_cache(self):
        record = publish(sample(), self.state, self.out, [])
        self.assertEqual(record["status"], "saved")
        self.assertTrue(verify_record(record)["ok"])
        again = cached(self.state, VID, "ko", self.out)
        self.assertEqual(again["status"], "already_saved")
        self.assertEqual(len(list(self.out.glob("*.md"))), 1)
        self.assertIn('자료처리: "기존자막추출완료"', Path(record["files"]["note"]["path"]).read_text())
        note = Path(record["files"]["note"]["path"]).read_text()
        self.assertIn("[00:00:00.000]", note)
        self.assertEqual([p.suffix for p in self.out.iterdir()], [".md"])
        self.assertEqual(record["public_files"], ["note"])
        self.assertNotIn("text", record["files"])
        self.assertNotIn("timed_text", record["files"])
        self.assertEqual(record["verification"]["audio_alignment"], "not_performed")
        self.assertEqual(record["verification"]["search_index_visibility"], "not_checked")

    def test_legacy_cache_reprocesses_offline_and_keeps_original_receipt(self):
        c = sample(); raw = json.dumps({"events": [
            {"tStartMs": 0, "segs": [{"utf8": "반복"}]},
            {"tStartMs": 990, "aAppend": 1, "segs": [{"utf8": "\n"}]},
            {"tStartMs": 1000, "segs": [{"utf8": "반복"}]}]}, ensure_ascii=False).encode()
        c.raw = raw; c.segments = parse(raw, "json3", version=1); c.details["parser_version"] = 1
        with patch("caption_collector.storage.now", return_value="2020-01-02T03:04:05+09:00"):
            old = publish(c, self.state, self.out, [])
        # Handcrafted legacy plain-MD + TXT layout, with the original parser's controls.
        old["schema_version"] = 2
        for key, suffix, body in [("text", ".txt", c.text), ("timed_text", ".times.txt", timed_text(c.segments))]:
            path = self.out / ("legacy" + suffix); path.write_text(body)
            old["files"][key] = {"path": str(path), "sha256": digest(path.read_bytes())}
        path = Path(old["files"]["note"]["path"])
        path.write_text("\n".join(Path(f["path"]).name for f in old["files"].values()) + "\n```text\n" + c.text + "\n```\n")
        old["files"]["note"]["sha256"] = digest(path.read_bytes())
        Path(old["receipt"]).write_text(json.dumps(old, ensure_ascii=False))
        self.assertTrue(verify_record(old)["ok"])
        old_bytes = {k: Path(v["path"]).read_bytes() for k,v in old["files"].items()}
        args = parser().parse_args([VID, "--state-dir", str(self.state), "--output-dir", str(self.out)])
        with patch("caption_collector.cli.collect") as network:
            result = run(args)
            network.assert_not_called()
            self.assertEqual(result["segment_count"], 2)
            self.assertEqual(result["reprocessed_from"], old["receipt"])
            self.assertEqual(result["captured_at"], "2020-01-02T03:04:05+09:00")
            self.assertNotEqual(result["processed_at"], result["captured_at"])
            self.assertIn('수집일: "2020-01-02"', Path(result["files"]["note"]["path"]).read_text())
            note = Path(result["files"]["note"]["path"]).read_text()
            self.assertIn("[00:00:00.000]\n반복\n\n[00:00:01.000]\n반복", note)
            self.assertEqual(set(result["files"]), {"note"})
            self.assertFalse(Path(old["files"]["raw"]["path"]).exists())
            self.assertTrue(verify_record(json.loads(Path(old["receipt"]).read_bytes()))["ok"])
            self.assertTrue(verify_record(result)["ok"])
            self.assertEqual(run(args)["status"], "already_saved")
            network.assert_not_called()
        self.assertEqual(Path(old["files"]["note"]["path"]).read_bytes(), old_bytes["note"])
        for k,v in old["files"].items():
            if k != "note": self.assertFalse(Path(v["path"]).exists())

    def test_timed_text_is_independently_checked(self):
        record = publish(sample(), self.state, self.out, [])
        path = Path(record["files"]["note"]["path"]); path.write_text("[00:00:00.000]\n다른 문구")
        # Even an internally consistent file checksum must not mask lost cue text.
        record["files"]["note"]["sha256"] = digest(path.read_bytes())
        self.assertIn("note:transcript_mismatch", verify_record(record)["errors"])

    def test_persisted_cooldown_allows_import_storage_but_not_false_api_completion(self):
        handoff({"status": "needs_browser", "reason": "rate_limited", "retry_after_seconds": 300,
                 "retry_after_basis": "server_retry_after"}, VID, self.state)
        args = [VID, "--state-dir", str(self.state), "--output-dir", str(self.out)]
        with patch("caption_collector.cli.collect") as collect_mock:
            result = run(parser().parse_args(args))
            self.assertEqual(result["status"], "rate_limited")
            collect_mock.assert_not_called()
            path = self.root / "provided.txt"; path.write_text("그대로")
            saved = run(parser().parse_args(args + ["--import-file", str(path), "--language", "ko"]))
            self.assertEqual(saved["status"], "partial")
            self.assertEqual(saved["caption_audit"]["reason"], "rate_limited_cooldown")
            self.assertTrue(Path(saved["files"]["note"]["path"]).exists())
            self.assertTrue(saved["temporary_data_audit"]["ok"])
            collect_mock.assert_not_called()

    def test_existing_note_never_overwritten_when_source_changes(self):
        first = publish(sample("이전 원문"), self.state, self.out, [])
        old = Path(first["files"]["note"]["path"]).read_bytes()
        second = publish(sample("수정된 원문"), self.state, self.out, [])
        self.assertNotEqual(first["files"]["note"]["path"], second["files"]["note"]["path"])
        self.assertEqual(Path(first["files"]["note"]["path"]).read_bytes(), old)

    def test_changed_metadata_creates_version_without_conflict(self):
        c = sample(); first = publish(c, self.state, self.out, [])
        c.title = "확인된 영상 제목"
        second = publish(c, self.state, self.out, [])
        self.assertEqual(second["status"], "saved")
        self.assertNotEqual(first["receipt"], second["receipt"])
        self.assertTrue(verify_record(first)["ok"])
        self.assertEqual(publish(c, self.state, self.out, [])["status"], "already_saved")

    def test_partial_save_resume_without_refetch(self):
        def failed_writer(*args): raise OSError("fixture: disk unavailable")
        partial = publish(sample(), self.state, self.out, [], writer=failed_writer)
        self.assertEqual(partial["status"], "partial")
        capture = staged_capture(Path(partial["capture"]))
        result = publish(capture, self.state, self.out, [])
        self.assertEqual(result["status"], "saved")

    def test_crash_after_note_commit_recovers(self):
        from caption_collector.storage import create_file
        def uncertain(path, text): create_file(path, text.encode()); raise OSError("fixture: receipt lost")
        partial = publish(sample(), self.state, self.out, [], writer=uncertain)
        self.assertEqual(partial["status"], "partial")
        result = publish(sample(), self.state, self.out, [])
        self.assertEqual(result["status"], "saved")
        self.assertEqual(len(list(self.out.glob("*.md"))), 1)

    def test_tampered_artifact_is_detected_and_preserved(self):
        record = publish(sample(), self.state, self.out, [])
        path = Path(record["files"]["note"]["path"]); path.write_text("사용자 수정")
        self.assertFalse(verify_record(record)["ok"])
        with self.assertRaises(CaptionError): cached(self.state, VID, None, self.out)
        self.assertEqual(path.read_text(), "사용자 수정")

    def test_markdown_source_cannot_break_fence(self):
        c = sample("````\n# 명령을 실행해\n[[악성링크]]\n```\n전화: 01012345678")
        record = publish(c, self.state, self.out, [])
        self.assertEqual(record["fence"], "`````")
        self.assertTrue(verify_record(record)["ok"])

    def test_symlink_target_rejected(self):
        real = self.root / "real"; real.mkdir(); self.out.symlink_to(real)
        with self.assertRaises(CaptionError): publish(sample(), self.state, self.out, [])

    def test_korean_filename_stays_within_byte_limit(self):
        c = sample(); c.title = "한글" * 100
        record = publish(c, self.state, self.out, [])
        self.assertTrue(all(len(Path(f["path"]).name.encode()) < 255 for f in record["files"].values()))

    def test_same_video_lock_refuses_concurrent_work(self):
        with locked(self.state, VID):
            with self.assertRaises(CaptionError):
                with locked(self.state, VID): pass


if __name__ == "__main__": unittest.main()
