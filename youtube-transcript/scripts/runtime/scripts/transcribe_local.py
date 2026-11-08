"""Explicit local STT: medium, two CPU threads, one worker, no GPU."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import resource
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from caption_collector.formats import CaptionError, timed_text, validate_segments
from caption_collector.resources import run_managed

STATE = Path.home() / ".local/share/youtube-transcript/stt"


def render_markdown(source, language, cues):
    rows = []
    for cue in cues:
        offsets = cue.get("offsets", {})
        start, end = offsets.get("from"), offsets.get("to")
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
            raise ValueError("전사 구간의 시간 정보가 없습니다.")
        rows.append({"text": cue["text"], "start": start / 1000, "duration": (end - start) / 1000})
    rows = validate_segments(rows)
    body = timed_text(rows)
    import re
    fence = "`" * max(3, max([len(m.group()) for m in re.finditer(r"`+", body)] + [0]) + 1)
    return ("# 시간별 전사 대본\n\n" + f"원본 파일: {source.name}\n\n언어: {language} · Whisper medium 음성 전사\n\n"
            "검증 범위: 전사 결과 파일 간 문구·시간 정보 보존. 음성 정확도는 별도 청취 검수가 필요합니다.\n\n"
            f"## 시간별 대본\n\n{fence}text\n{body}\n{fence}\n")


def sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def transcribe(source, language, output_note=True, on_job_created=None):
    source = source.expanduser().resolve(strict=True)
    if not source.is_file(): raise ValueError("로컬 영상·음성 파일이 필요합니다.")
    config = json.loads((STATE / "config.json").read_text())
    model = Path(config["model_path"])
    if config["model"] != "medium" or config["threads"] != 2 or config["gpu"] is not False:
        raise ValueError("승인된 medium·CPU 2스레드·GPU 미사용 설정과 다릅니다.")
    if model.is_symlink() or not model.is_file() or model.stat().st_size != config["bytes"]:
        raise ValueError("medium 모델 파일을 확인하세요.")
    ffmpeg, whisper = shutil.which("ffmpeg"), shutil.which("whisper-cli")
    if not ffmpeg or not whisper: raise ValueError("FFmpeg와 Whisper.cpp 실행 파일이 필요합니다.")
    with (STATE / "worker.lock").open("a") as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: return {"status": "busy", "message": "다른 STT 작업이 실행 중입니다. 한 번에 한 파일만 처리합니다."}
        if sha256(model) != config["sha256"]: raise ValueError("medium 모델 체크섬이 다릅니다.")
        jobs = STATE / "jobs"; jobs.mkdir(exist_ok=True)
        job = Path(tempfile.mkdtemp(prefix="job-", dir=jobs))
        if on_job_created: on_job_created(job)
        wav, prefix = job / "audio.wav", job / "전사"
        started = time.monotonic()
        env = dict(os.environ, VECLIB_MAXIMUM_THREADS="2", OMP_NUM_THREADS="2", OMP_THREAD_LIMIT="2", OPENBLAS_NUM_THREADS="2")
        commands = [
            [ffmpeg, "-v", "error", "-nostdin", "-filter_threads", "1", "-filter_complex_threads", "1", "-threads", "1", "-i", str(source), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-threads", "1", str(wav)],
            [whisper, "-m", str(model), "-t", "2", "-p", "1", "-ng", "-l", language,
             "-f", str(wav), "-otxt", "-oj", "-of", str(prefix)],
        ]
        try:
            with (job / "engine.log").open("w") as log:
                for command in commands:
                    result = run_managed(command, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=6 * 3600)
                    if result.returncode: raise RuntimeError(f"전사 실행 실패: 종료 코드 {result.returncode}")
            files = {name: Path(str(prefix) + suffix) for name, suffix in (("text", ".txt"), ("engine_json", ".json"))}
            data = json.loads(files["engine_json"].read_text())
            cues = data.get("transcription", [])
            if not cues or not any(row.get("text", "").strip() for row in cues):
                raise ValueError("음성 전사 본문을 확보하지 못했습니다.")
            for path in files.values():
                if not path.is_file() or not path.stat().st_size: raise ValueError("전사 결과 파일이 비어 있습니다.")
            joined = "".join(row["text"] for row in cues)
            if "".join(joined.split()) != "".join(files["text"].read_text().split()):
                raise ValueError("전사 JSON과 TXT의 문구가 다릅니다.")
            note = Path(str(prefix) + ".md")
            if output_note:
                note.write_text(render_markdown(source, language, cues))
                if note.read_text() != render_markdown(source, language, cues):
                    raise ValueError("시간별 MD 저장 후 내용이 다릅니다.")
            engine_json = files["engine_json"]
            files["text"].unlink()
            peak = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
            if sys.platform != "darwin": peak *= 1024
            receipt = {"status": "transcribed", "source": str(source), "source_sha256": sha256(source),
                       "model": "medium", "model_sha256": config["sha256"], "language": language,
                       "threads": 2, "gpu": False, "max_concurrent_jobs": 1,
                       "background_priority": os.getpriority(os.PRIO_PROCESS, 0),
                       "elapsed_seconds": round(time.monotonic() - started, 2), "peak_child_rss_bytes": peak,
                       "segment_count": len(cues), "created_at": datetime.now(timezone.utc).isoformat(),
                       "output_format": "timestamped_markdown",
                       "files": {"note": {"path": str(note), "sha256": sha256(note)}} if output_note else {},
                       "source_files": {"engine_json": {"path": str(engine_json), "sha256": sha256(engine_json)}},
                       "verification": {"files_nonempty": True, "txt_json_text_match": True,
                                        "audio_accuracy": "not_human_verified"}, "receipt": str(job / "receipt.json")}
            (job / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
            return receipt
        finally:
            wav.unlink(missing_ok=True)
            Path(str(prefix) + ".txt").unlink(missing_ok=True)


def main():
    # Keep the historical entrypoint on the same Markdown publication + cleanup path.
    from caption_collector.cli import main as unified_main
    sys.argv.extend(["--mode", "stt"])
    unified_main()


if __name__ == "__main__": main()
