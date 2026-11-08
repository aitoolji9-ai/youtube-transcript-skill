import fcntl
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("local_stt", Path(__file__).resolve().parents[1] / "youtube-transcript/scripts/runtime/scripts/transcribe_local.py")
stt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stt)


class LocalSttTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "input.wav"; self.source.write_bytes(b"original")
        self.model = self.root / "model.bin"; self.model.write_bytes(b"model")
        self.state = self.root / "state"; self.state.mkdir()
        self.config = {"model": "medium", "model_path": str(self.model), "bytes": 5,
                       "sha256": stt.sha256(self.model), "threads": 2, "gpu": False}
        (self.state / "config.json").write_text(json.dumps(self.config))
        self.state_patch = patch.object(stt, "STATE", self.state); self.state_patch.start()
        self.which = patch.object(stt.shutil, "which", side_effect=lambda n: "/observed/" + n); self.which.start()

    def tearDown(self):
        self.which.stop(); self.state_patch.stop(); self.temp.cleanup()

    def test_second_worker_is_rejected_before_starting_engine(self):
        with (self.state / "worker.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(stt, "run_managed") as run:
                self.assertEqual(stt.transcribe(self.source, "ko")["status"], "busy")
                run.assert_not_called()

    def test_corrupt_model_never_starts_engine(self):
        self.model.write_bytes(b"other")
        with patch.object(stt, "run_managed") as run:
            with self.assertRaisesRegex(ValueError, "체크섬"): stt.transcribe(self.source, "ko")
            run.assert_not_called()

    def test_engine_failure_removes_decoded_audio_and_preserves_input(self):
        def engine(command, **kwargs):
            if command[0].endswith("ffmpeg"):
                Path(command[-1]).write_bytes(b"decoded"); return SimpleNamespace(returncode=0)
            return SimpleNamespace(returncode=1)
        with patch.object(stt, "run_managed", side_effect=engine):
            with self.assertRaises(RuntimeError): stt.transcribe(self.source, "ko")
        self.assertFalse(list((self.state / "jobs").glob("*/audio.wav")))
        self.assertEqual(self.source.read_bytes(), b"original")

    def test_inconsistent_transcript_cannot_be_reported_as_success(self):
        def engine(command, **kwargs):
            if command[0].endswith("ffmpeg"):
                Path(command[-1]).write_bytes(b"decoded")
            else:
                prefix = Path(command[-1])
                Path(str(prefix) + ".txt").write_text("틀린 숫자 30")
                Path(str(prefix) + ".srt").write_text("1\n00:00:00,000 --> 00:00:01,000\n숫자 3\n")
                Path(str(prefix) + ".json").write_text(json.dumps({"transcription": [{"text": "숫자 3"}]}))
            return SimpleNamespace(returncode=0)
        with patch.object(stt, "run_managed", side_effect=engine):
            with self.assertRaisesRegex(ValueError, "문구가 다릅니다"): stt.transcribe(self.source, "ko")
        self.assertFalse(list((self.state / "jobs").glob("*/receipt.json")))
        self.assertFalse(list((self.state / "jobs").glob("*/audio.wav")))

    def test_success_exports_only_timestamped_markdown(self):
        def engine(command, **kwargs):
            if command[0].endswith("ffmpeg"):
                Path(command[-1]).write_bytes(b"decoded")
            else:
                prefix = Path(command[-1])
                Path(str(prefix) + ".txt").write_text(" 그대로\n다시")
                Path(str(prefix) + ".json").write_text(json.dumps({"transcription": [
                    {"text": " 그대로\n다시", "offsets": {"from": 1240, "to": 4000}}]}))
            return SimpleNamespace(returncode=0)
        with patch.object(stt, "run_managed", side_effect=engine):
            result = stt.transcribe(self.source, "ko")
        self.assertEqual(set(result["files"]), {"note"})
        note = Path(result["files"]["note"]["path"])
        self.assertIn("[00:00:01.240]\n 그대로\n다시", note.read_text())
        self.assertFalse(list(note.parent.glob("*.txt")))
        self.assertFalse(list(note.parent.glob("*.srt")))


if __name__ == "__main__": unittest.main()
