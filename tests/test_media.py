import fcntl
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from caption_collector.cli import can_fallback_to_media, mode_matches, parser, run
from caption_collector.formats import CaptionError, Capture
from caption_collector.media import geometric_line, local_capture, merge_observations, region_filter, select_subtitle, source_info
from caption_collector.resources import media_slot
from caption_collector.storage import publish, rebuild_record, verify_record


class MediaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name).resolve()
        self.source = self.root / "source.mp4"; self.source.write_bytes(b"unchanged source")
        self.state = self.root / "state"; self.output = self.root / "output"
    def tearDown(self): self.temp.cleanup()

    def test_access_restrictions_never_trigger_audio_download(self):
        for reason in ["rate_limited", "request_blocked", "needs_login", "access_denied", "live_in_progress", "unavailable", "needs_language"]:
            self.assertFalse(can_fallback_to_media({"reason": "automatic_routes_exhausted"}, [{"outcome": reason}]))
        self.assertTrue(can_fallback_to_media({"reason": "automatic_routes_exhausted"}, [{"outcome": "no_tracks_reported"}]))
        self.assertFalse(can_fallback_to_media({"reason": "rate_limited"}, []))

    def test_embedded_caption_is_used_without_starting_stt_or_ocr(self):
        info = {"duration": 2, "streams": [{"index": 2, "codec_type": "subtitle", "codec_name": "mov_text", "tags": {"language": "kor"}}, {"codec_type": "audio"}]}
        def extract(args, **kw):
            self.assertIn("0:2", args)
            Path(args[-1]).write_text("WEBVTT\n\n00:00:00.240 --> 00:00:01.500\n원문 그대로\n")
            return ""
        with patch("caption_collector.media.probe", return_value=info), patch("caption_collector.media.command", side_effect=extract), patch("caption_collector.media.transcribe") as stt, patch("caption_collector.media.ocr") as ocr:
            capture, _ = local_capture(self.source, self.state, language="ko")
        stt.assert_not_called();ocr.assert_not_called()
        self.assertEqual(capture.provider, "embedded_subtitle")
        self.assertEqual(capture.segments, [{"text": "원문 그대로", "start": .24, "duration": 1.26}])
        self.assertEqual(capture.metadata()["url"], str(self.source))
        record = publish(capture, self.state, self.output, [])
        self.assertTrue(verify_record(record)["ok"])
        self.assertEqual([f.suffix for f in self.output.iterdir()], [".md"])
        self.assertEqual(rebuild_record(record).details["local_source"]["sha256"], capture.details["local_source"]["sha256"])

    def test_audio_without_subtitles_selects_stt(self):
        rows = [{"text": "한국어 전사", "start": 0, "duration": 1}]
        raw = self.root / "engine.json";raw.write_text("{}")
        from caption_collector.media import file_hash
        evidence = {"engine_json": str(raw), "sha256": file_hash(raw), "detected_language": "ko"}
        with patch("caption_collector.media.probe", return_value={"duration": 1,"streams": [{"codec_type": "audio"}]}), patch("caption_collector.media.transcribe", return_value=(rows,evidence)), patch("caption_collector.media.ocr") as ocr:
            capture, _ = local_capture(self.source, self.state)
        ocr.assert_not_called();self.assertEqual(capture.provider, "whisper_medium");self.assertEqual(capture.language,"ko")
        r = publish(capture, self.state, self.output, []);self.assertTrue(verify_record(r)["ok"])
        raw.write_text("changed engine evidence");self.assertFalse(verify_record(r)["ok"])

    def test_mute_video_selects_screen_ocr(self):
        evidence = {"fps": 2, "region": "full", "audit_files": []}
        with patch("caption_collector.media.probe", return_value={"duration":1,"streams":[{"codec_type":"video"}]}), patch("caption_collector.media.ocr",return_value=([{"text":"화면 글자","start":0,"duration":1}],evidence)), patch("caption_collector.media.transcribe") as stt:
            c,_ = local_capture(self.source,self.state)
        stt.assert_not_called();self.assertEqual(c.provider,"screen_ocr")
        record=publish(c,self.state,self.output,[]);self.assertTrue(verify_record(record)["ok"])

    def test_same_screen_text_after_a_gap_is_not_deduplicated(self):
        frames=[{"text":"반복","at":0},{"text":"반복","at":.5},{"text":"","at":1},{"text":"반복","at":1.5}]
        self.assertEqual(merge_observations(frames,.5,2),[{"text":"반복","start":0,"duration":1},{"text":"반복","start":1.5,"duration":.5}])

    def test_word_boxes_preserve_real_spaces_despite_overlapping_boxes(self):
        words=[{"text":t,"left":str(x),"width":str(w)} for t,x,w in [("첫",557,49),("번째",631,101),("자",756,157),("막",822,53),("입니다.",926,113)]]
        self.assertEqual(geometric_line(words),"첫 번째 자막입니다.")

    def test_region_validation_and_ambiguous_subtitles(self):
        for value in ["-1,0,1,1","0,0,2,1","0,0,nan,1","garbage"]:
            with self.assertRaises(CaptionError):region_filter(value)
        self.assertTrue(region_filter("bottom").startswith("crop="))
        tracks=[{"index":i,"codec_type":"subtitle","tags":{"language":lang}} for i,lang in enumerate(["kor","eng"])]
        with self.assertRaises(CaptionError):select_subtitle(tracks)
        self.assertEqual(select_subtitle(tracks,"ko")["index"],0)

    def test_one_shared_worker_for_all_media_methods(self):
        with patch("caption_collector.resources.lower_priority"):
            with media_slot(self.state):
                with self.assertRaises(CaptionError) as e:
                    with media_slot(self.state):pass
                self.assertEqual(e.exception.kind,"busy")

    def test_explicit_method_does_not_reuse_other_methods_cache(self):
        self.assertFalse(mode_matches({"provider":"whisper_medium"},"ocr"))
        self.assertFalse(mode_matches({"provider":"screen_ocr"},"captions"))
        self.assertTrue(mode_matches({"provider":"screen_ocr"},"auto"))
        self.assertFalse(mode_matches({"provider":"screen_ocr","extraction_options":{"fps":2.0,"region":"bottom"}},"ocr",2.0,"full"))

    def test_auto_youtube_fallback_is_dispatched_only_after_allowed_exhaustion(self):
        args=parser().parse_args(["https://youtu.be/jKupp4CYJAc","--state-dir",str(self.state),"--output-dir",str(self.output)])
        no_tracks=[{"provider":"yt_dlp","outcome":"no_tracks_reported"}]
        result={"status":"needs_browser","reason":"automatic_routes_exhausted"}
        with patch("caption_collector.cli.collect",return_value=(None,no_tracks,result)),patch("caption_collector.cli.youtube_media",return_value={"status":"saved"}) as media:
            self.assertEqual(run(args)["status"],"saved");media.assert_called_once()
        for denial in ["rate_limited","request_blocked","access_denied"]:
            with patch("caption_collector.cli.collect",return_value=(None,[{"outcome":denial}],result)),patch("caption_collector.cli.youtube_media") as media:
                self.assertEqual(run(args)["reason"],"automatic_routes_exhausted");media.assert_not_called()

    def test_mutated_embedded_media_is_not_published(self):
        info={"duration":1,"streams":[{"index":0,"codec_type":"subtitle","codec_name":"mov_text","tags":{"language":"kor"}}]}
        def extract(args,**kwargs):
            Path(args[-1]).write_text("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\n자막\n")
            self.source.write_bytes(b"source changed while extracting")
        with patch("caption_collector.media.probe",return_value=info),patch("caption_collector.media.command",side_effect=extract):
            with self.assertRaises(CaptionError) as e:local_capture(self.source,self.state)
        self.assertEqual(e.exception.kind,"source_changed")


if __name__ == "__main__":unittest.main()
