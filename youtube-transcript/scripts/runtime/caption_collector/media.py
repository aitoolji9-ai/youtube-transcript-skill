"""Local media and authorized YouTube fallback, sharing the MD publisher."""
from __future__ import annotations

import hashlib
import csv
import io
import statistics
import importlib.util
import json
import math
from pathlib import Path
import shutil
import subprocess
import tempfile

from .formats import CaptionError, Capture, digest, parse, validate_segments
from .providers import QuietLogger, classify
from .resources import child_env, run_managed
from .storage import PROJECT, STATE, atomic, json_bytes

TEXT_SUBTITLES = {"subrip", "srt", "ass", "ssa", "webvtt", "mov_text", "text", "ttml"}
ALIASES = {"kor": "ko", "eng": "en", "jpn": "ja", "zho": "zh", "chi": "zh"}


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(4 * 1024 * 1024), b""): h.update(block)
    return h.hexdigest()


def command(args, *, input=None, timeout=3600):
    try:
        result = run_managed(args, input=input, text=True, capture_output=True,
                                env=child_env(), timeout=timeout)
    except subprocess.TimeoutExpired:
        raise CaptionError("media_timeout", "미디어 처리 시간이 초과됐습니다. 확보한 내부 원본은 보존했습니다.")
    if result.returncode:
        raise CaptionError("media_tool_failed", "미디어 처리 도구가 실패했습니다.", tool=Path(args[0]).name,
                           returncode=result.returncode)
    return result.stdout


def probe(path):
    tool = shutil.which("ffprobe")
    if not tool: raise CaptionError("missing_dependency", "FFprobe가 필요합니다.")
    raw = command([tool, "-v", "error", "-show_entries",
                   "format=duration:stream=index,codec_type,codec_name,duration:stream_tags=language,title",
                   "-of", "json", str(path)], timeout=60)
    data = json.loads(raw)
    values = [data.get("format", {}).get("duration")] + [s.get("duration") for s in data.get("streams", [])]
    durations = []
    for value in values:
        try:
            n = float(value)
            if math.isfinite(n) and n > 0: durations.append(n)
        except (ValueError, TypeError): pass
    data["duration"] = max(durations, default=None)
    return data


def select_subtitle(streams, language=None):
    tracks = [s for s in streams if s.get("codec_type") == "subtitle"]
    if not tracks: return None
    for s in tracks:
        tag = s.get("tags", {}).get("language", "und").lower()
        s["language"] = ALIASES.get(tag, tag)
    if language and language != "auto":
        found = [s for s in tracks if s["language"] == language or s["language"].split("-")[0] == language]
        if not found: raise CaptionError("needs_language", "요청한 언어의 내장 자막이 없습니다.",
                                        tracks=[{"index": s["index"], "language": s["language"]} for s in tracks])
        tracks = found
    elif len({s["language"] for s in tracks}) > 1:
        raise CaptionError("needs_language", "여러 언어의 내장 자막 중 언어 선택이 필요합니다.",
                           tracks=[{"index": s["index"], "language": s["language"]} for s in tracks])
    return sorted(tracks, key=lambda s: (s.get("codec_name") not in TEXT_SUBTITLES, s["index"]))[0]


def source_info(path):
    path = Path(path).expanduser().resolve(strict=True)
    if not path.is_file(): raise CaptionError("invalid_file", "로컬 영상·음성 파일이 필요합니다.")
    before = path.stat()
    sha = file_hash(path)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise CaptionError("source_changed", "해시 확인 중 원본 파일이 변경됐습니다.")
    return path, {"path": str(path), "sha256": sha, "bytes": before.st_size, "mtime_ns": before.st_mtime_ns}


def stt_rows(path):
    spec = importlib.util.spec_from_file_location("caption_local_stt", PROJECT / "scripts/transcribe_local.py")
    engine = importlib.util.module_from_spec(spec); spec.loader.exec_module(engine)
    # The outer media worker owns the global slot. The engine retains its model lock.
    return engine


def transcribe(path, language, work, source_sha=None):
    config = json.loads((STATE / "stt/config.json").read_bytes())
    signature = {"source_sha256": source_sha or file_hash(path), "model_sha256": config["sha256"], "language": language or "auto"}
    checkpoint = work / "stt.json"
    pending = work / "stt-pending.json"
    if checkpoint.exists():
        saved = json.loads(checkpoint.read_bytes())
        if saved["signature"] == signature:
            raw_path = Path(saved["engine_json"])
            if raw_path.is_file() and file_hash(raw_path) == saved["sha256"]:
                pending.unlink(missing_ok=True)
                return saved["rows"], saved
    if pending.exists():
        interrupted = json.loads(pending.read_bytes())
        job = Path(interrupted["job"])
        if job.parent != STATE / "stt/jobs" or job.is_symlink() or not job.name.startswith("job-") or any(job.glob("*.md")):
            raise CaptionError("unsafe_cleanup", "중단된 음성 전사의 정리 위치가 올바르지 않습니다.")
        if job.exists(): shutil.rmtree(job)
        pending.unlink()
    selected_language = (language or "auto").lower().split("-")[0]
    selected_language = ALIASES.get(selected_language, selected_language)
    result = stt_rows(path).transcribe(path, selected_language, output_note=False,
                                     on_job_created=lambda job: atomic(pending, json_bytes({"job": str(job), "signature": signature})))
    if result["status"] != "transcribed": raise CaptionError(result["status"], result.get("message", "음성 전사 실패"))
    if result["source_sha256"] != signature["source_sha256"]:
        raise CaptionError("source_changed", "전사 중 원본 파일이 변경됐습니다.")
    original = result["source_files"]["engine_json"]
    data = json.loads(Path(original["path"]).read_bytes())
    rows = []
    for cue in data["transcription"]:
        offsets = cue["offsets"]
        rows.append({"text": cue["text"], "start": offsets["from"] / 1000,
                     "duration": (offsets["to"] - offsets["from"]) / 1000})
    rows = validate_segments(rows)
    saved = {"signature": signature, "rows": rows, "engine_json": original["path"], "sha256": original["sha256"],
             "detected_language": data.get("result", {}).get("language") or language,
             "model": "medium", "threads": 2, "gpu": False, "elapsed_seconds": result["elapsed_seconds"],
             "peak_child_rss_bytes": result["peak_child_rss_bytes"]}
    atomic(checkpoint, json_bytes(saved))
    pending.unlink(missing_ok=True)
    return rows, saved


def ensure_ocr():
    binary = shutil.which("tesseract")
    models = STATE / "ocr-models"
    if binary and (models / "manifest.json").is_file() and (models / "eng.traineddata").is_file():
        manifest = json.loads((models / "manifest.json").read_bytes())
        if file_hash(models / "kor.traineddata") != manifest["sha256"]:
            raise CaptionError("verification_failed", "한국어 OCR 모델 체크섬이 다릅니다.")
        return Path(binary)
    raise CaptionError("missing_dependency", "Tesseract와 검증된 한국어·영어 OCR 모델이 필요합니다.")


def recognize_image(binary, image, language):
    raw = command([str(binary), str(image), "stdout", "--tessdata-dir", str(STATE / "ocr-models"),
                   "-l", "eng" if language and language.startswith("en") else "kor+eng",
                   "--oem", "1", "--psm", "11", "-c", "tessedit_create_tsv=1"], timeout=60)
    groups = {}
    for row in csv.DictReader(io.StringIO(raw), delimiter="\t"):
        if row.get("level") != "5" or not row.get("text", "").strip(): continue
        key = tuple(row[k] for k in ("page_num", "block_num", "par_num", "line_num"))
        groups.setdefault(key, []).append(row)
    return [{"text": geometric_line(rows), "words": rows,
             "confidence": min(float(r["conf"]) for r in rows) / 100,
             "box": [int(rows[0]["left"]), int(rows[0]["top"]),
                     max(int(r["left"]) + int(r["width"]) for r in rows),
                     max(int(r["top"]) + int(r["height"]) for r in rows)]} for rows in groups.values()]


def geometric_line(words):
    """Recover visible spacing from word boxes, retaining the raw TSV words."""
    words = sorted(words, key=lambda r: int(r["left"]))
    units = [int(r["width"]) / max(1, sum(c.isalnum() for c in r["text"])) for r in words]
    threshold = max(2, statistics.median(units) * .3)
    result, end = "", None
    for word in words:
        left = int(word["left"])
        if end is not None and left - end > threshold: result += " "
        result += word["text"]
        end = max(end or 0, left + int(word["width"]))
    return result


def region_filter(region):
    if region == "full": return ""
    if region == "bottom": values = [0, .55, 1, .45]
    else:
        try: values = [float(n) for n in region.split(",")]
        except ValueError: raise CaptionError("invalid_region", "OCR 영역은 full, bottom 또는 x,y,w,h 비율입니다.")
    if len(values) != 4 or any(not math.isfinite(n) for n in values):
        raise CaptionError("invalid_region", "OCR 영역 값이 올바르지 않습니다.")
    x, y, w, h = values
    if min(x, y) < 0 or min(w, h) <= 0 or x + w > 1 or y + h > 1:
        raise CaptionError("invalid_region", "OCR 영역은 영상 범위 안의 0~1 비율이어야 합니다.")
    return f"crop=iw*{w}:ih*{h}:iw*{x}:ih*{y},"


def merge_observations(observations, interval, duration):
    rows = []
    current = None
    for frame in observations:
        text, at = frame["text"], frame["at"]
        if text and current and current["text"] == text and at <= current["start"] + current["duration"] + .001:
            current["duration"] = min(duration, at + interval) - current["start"]
        else:
            current = {"text": text, "start": at, "duration": min(duration, at + interval) - at} if text else None
            if current: rows.append(current)
    return validate_segments(rows)


def ocr(path, info, work, fps=2.0, region="full", overlay=None, language=None, source_sha=None):
    if language and language.lower().split("-")[0] not in {"ko", "en", "auto", "und"}:
        raise CaptionError("unsupported_ocr_language", "현재 화면 OCR 모델은 한국어·영어입니다.", supported_languages=["ko", "en"])
    duration = info["duration"]
    if not duration: raise CaptionError("unknown_duration", "전체 화면을 검사하려면 영상 길이를 확인해야 합니다.")
    if not .5 <= fps <= 10: raise CaptionError("invalid_fps", "OCR 검사 빈도는 초당 0.5~10회입니다.")
    crop = region_filter(region)
    binary = ensure_ocr()
    # The global guard retains the media lock until any orphaned tool has stopped.
    for stale in work.glob("frames-*"):
        if stale.is_symlink(): raise CaptionError("unsafe_cleanup", "OCR 임시 폴더가 심볼릭 링크입니다.")
        if stale.is_dir(): shutil.rmtree(stale)
    interval = 1 / fps
    observations, audit_files = [], []
    index_path = work / "ocr-index.json"
    index = json.loads(index_path.read_bytes()) if index_path.exists() else {}
    source_sha = source_sha or file_hash(path)
    engine_sha = file_hash(binary)
    model_signature = file_hash(STATE / "ocr-models/kor.traineddata") + file_hash(STATE / "ocr-models/eng.traineddata")
    previous_hash, previous_rows = None, []
    for chunk, start in enumerate(range(0, math.ceil(duration), 30)):
        checkpoint = work / f"ocr-{chunk:06}.json"
        signature = {"source_sha256": source_sha, "fps": fps, "region": region,
                     "overlay": overlay, "engine": engine_sha, "models": model_signature, "language": language,
                     "spacing_version": 3}
        if checkpoint.exists() and checkpoint.name in index:
            if file_hash(checkpoint) != index[checkpoint.name]:
                raise CaptionError("verification_failed", "OCR 재개 원본의 해시가 다릅니다.")
            saved = json.loads(checkpoint.read_bytes())
            if saved["signature"] != signature: raise CaptionError("checkpoint_conflict", "OCR 재개 조건이 이전 실행과 다릅니다.")
            frames = saved["frames"]
        else:
            with tempfile.TemporaryDirectory(prefix="frames-", dir=work) as tmp:
                pattern = str(Path(tmp) / "%06d.png")
                filters = crop + f"fps={fps},scale=min(1600\\,iw):-2"
                args = [shutil.which("ffmpeg"), "-v", "error", "-nostdin", "-filter_threads", "1", "-filter_complex_threads", "1", "-threads", "1", "-hwaccel", "none",
                        "-ss", str(start), "-i", str(path), "-t", str(min(30, duration - start)), "-an"]
                if overlay is not None:
                    args += ["-filter_complex", f"[0:v:0][0:{overlay}]overlay,{filters}[v]", "-map", "[v]"]
                else: args += ["-vf", filters]
                command(args + ["-threads", "1", pattern])
                images = sorted(Path(tmp).glob("*.png"))
                if not images: raise CaptionError("ocr_frames_missing", "화면 검사 프레임을 확보하지 못했습니다.")
                frames = []
                # Serial OCR per frame; exact duplicate images reuse the previous result.
                for i, image in enumerate(images):
                    at = start + i * interval
                    if at >= duration: break
                    sha = file_hash(image)
                    if sha != previous_hash:
                        previous_rows = recognize_image(binary, image, language)
                    previous_hash = sha
                    frames.append({"at": at, "text": "\n".join(r["text"] for r in previous_rows),
                                   "recognition": previous_rows, "frame_sha256": sha,
                                   "source_sha256": signature["source_sha256"]})
                    image.unlink()
                atomic(checkpoint, json_bytes({"signature": signature, "frames": frames}))
                index[checkpoint.name] = file_hash(checkpoint)
                atomic(index_path, json_bytes(index))
        observations.extend(frames)
        audit_files.append({"path": str(checkpoint), "sha256": file_hash(checkpoint)})
    rows = merge_observations(observations, interval, duration)
    return rows, {"engine": "Tesseract LSTM kor+eng", "cpu_only": True, "fps": fps, "region": region,
                  "sampled_frames": len(observations), "duration": duration,
                  "timestamps": "sample-based; transitions may differ by one sample interval",
                  "short_caption_limit": "Text displayed between samples may be missed.", "audit_files": audit_files}


def local_capture(path, state, *, method="auto", language=None, identity=None, fps=2.0, region="full", source_record=None):
    if source_record is None: path, source = source_info(path)
    else: path, source = Path(path).resolve(strict=True), source_record
    vid = identity or "local-" + source["sha256"][:20]
    info = probe(path)
    work = state / "media" / digest(json_bytes({"source": source["sha256"], "method": method,
                                                "language": language, "fps": fps, "region": region, "ocr_algorithm": 3}))[:24]
    work.mkdir(parents=True, exist_ok=True)
    details = {"source_uri": str(path), "local_source": source, "media_duration": info["duration"], "processing_work": str(work)}
    attempts = []
    track = select_subtitle(info.get("streams", []), language) if method in {"auto", "captions"} else None
    if track and track.get("codec_name") in TEXT_SUBTITLES:
        target = work / "embedded.vtt"
        command([shutil.which("ffmpeg"), "-v", "error", "-nostdin", "-threads", "1", "-i", str(path),
                 "-map", f"0:{track['index']}", "-c:s", "webvtt", "-y", str(target)], timeout=300)
        raw = target.read_bytes();rows = parse(raw, "vtt")
        if path.stat().st_size != source["bytes"] or path.stat().st_mtime_ns != source["mtime_ns"]:
            raise CaptionError("source_changed", "추출 중 원본 영상이 변경됐습니다. MD 저장을 중단했습니다.")
        details.update(embedded_stream=track["index"], method="embedded_subtitle")
        capture = Capture(vid, "embedded_subtitle", raw, "vtt", rows, track["language"], None,
                          path.stem, completeness="complete_embedded_subtitle_track", details=details)
        return capture, [{"provider": "embedded_subtitle", "outcome": "captured"}]
    if method == "captions": raise CaptionError("no_tracks_reported", "추출 가능한 텍스트 내장 자막이 없습니다.")
    audio = any(s.get("codec_type") == "audio" for s in info.get("streams", []))
    video = any(s.get("codec_type") == "video" for s in info.get("streams", []))
    selected = method
    if selected == "auto": selected = "ocr" if track or not audio else "stt"
    if selected == "stt":
        if not audio: raise CaptionError("no_audio", "음성 트랙이 없어 STT를 실행할 수 없습니다.")
        rows, evidence = transcribe(path, language, work, source["sha256"])
        provider, completeness = "whisper_medium", "complete_audio_transcription"
        details.update(method=provider, stt=evidence,
                       evidence_files=[{"path": evidence["engine_json"], "sha256": evidence["sha256"]}])
    else:
        if not video: raise CaptionError("no_video", "OCR에 사용할 영상 트랙이 없습니다.")
        rows, evidence = ocr(path, info, work, fps, region, track["index"] if track else None, language, source["sha256"])
        provider, completeness = "screen_ocr", "complete_sampled_screen_text"
        details.update(method=provider, ocr=evidence, evidence_files=evidence["audit_files"],
                       extraction_options={"fps": fps, "region": region})
    if path.stat().st_size != source["bytes"] or path.stat().st_mtime_ns != source["mtime_ns"]:
        raise CaptionError("source_changed", "처리 중 원본 영상이 변경됐습니다. MD 저장을 중단했습니다.")
    raw = json_bytes({"segments": rows, "provider": provider, "source": source, "evidence": evidence})
    actual_language = evidence.get("detected_language") if language in {None, "auto"} else language
    capture = Capture(vid, provider, raw, "json", rows, actual_language or "und", True, path.stem,
                      completeness=completeness, details=details)
    attempts.append({"provider": provider, "outcome": "captured"})
    return capture, attempts


def download_youtube(vid, state, *, video=False, browser=None, profile=None):
    import yt_dlp
    work = state / "downloads" / vid / ("video" if video else "audio")
    work.mkdir(parents=True, exist_ok=True)
    manifest = work / "download.json"
    if manifest.exists():
        saved = json.loads(manifest.read_bytes());path = Path(saved["path"])
        if path.parent == work and path.is_file() and file_hash(path) == saved["sha256"]: return path, saved
    opts = {"quiet": True, "no_warnings": True, "logger": QuietLogger(), "noplaylist": True,
            "cachedir": False, "socket_timeout": 20, "retries": 0, "fragment_retries": 0,
            "concurrent_fragment_downloads": 1, "ratelimit": 2 * 1024 * 1024,
            "outtmpl": str(work / "media.%(ext)s"),
            "format": "bestvideo[height<=1080]/bestvideo/best" if video else "bestaudio/best"}
    if browser:
        if browser not in {"chrome", "edge", "firefox", "safari", "brave", "chromium", "whale", "vivaldi", "opera"}:
            raise CaptionError("invalid_browser", "지원되는 브라우저 이름이 필요합니다.")
        opts["cookiesfrombrowser"] = (browser, profile)
    def guard(status):
        if shutil.disk_usage(work).free < 256 * 1024 * 1024:
            raise CaptionError("insufficient_disk", "다운로드를 위한 디스크 여유 공간이 부족합니다.")
    opts["progress_hooks"] = [guard]
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False)
            if not info or info.get("id") != vid: raise CaptionError("wrong_video", "영상 ID가 요청과 다릅니다.")
            if info.get("is_live"): raise CaptionError("live_in_progress", "진행 중인 라이브 전체를 저장하지 않습니다.")
            info = ydl.process_ie_result(info, download=True)
            path = Path(ydl.prepare_filename(info))
            if not path.is_file(): raise CaptionError("media_download_failed", "영상 음성 파일을 확보하지 못했습니다.")
            saved = {"path": str(path), "sha256": file_hash(path), "title": info.get("title"),
                     "channel": info.get("channel"), "language": info.get("language"),
                     "duration": info.get("duration"), "network_limit_bytes_per_second": 2 * 1024 * 1024}
            atomic(manifest, json_bytes(saved));return path, saved
    except Exception as exc: raise classify(exc)
