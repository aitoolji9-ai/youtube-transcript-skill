from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import unicodedata
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .formats import DETAIL_KEYS, PARSER_VERSION, CaptionError, Capture, digest, parse, timed_text, validate_segments

PROJECT = Path(__file__).resolve().parent.parent
LOCAL_OUTPUT = Path.home() / "Documents" / "대본MD"
STATE = Path.home() / ".local/share/youtube-transcript"


def now():
    return datetime.now(ZoneInfo("Asia/Seoul")).isoformat(timespec="seconds")


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()


def atomic(path: Path, raw: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise CaptionError("unsafe_path", "심볼릭 링크에 저장하지 않습니다.")
    fd, temp = tempfile.mkstemp(prefix=".caption-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw); f.flush(); os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp): os.unlink(temp)


def create_file(path: Path, raw: bytes):
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != raw:
            raise CaptionError("file_conflict", "기존 파일을 덮어쓰지 않습니다.", path=str(path))
        return
    # Stage on the same filesystem; ENOSPC must not poison the final filename.
    fd, temp = tempfile.mkstemp(prefix=".caption-stage-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw); f.flush(); os.fsync(f.fileno())
        try: os.link(temp, path)
        except FileExistsError:
            if path.is_symlink() or path.read_bytes() != raw:
                raise CaptionError("file_conflict", "기존 파일을 덮어쓰지 않습니다.", path=str(path))
    finally:
        if os.path.exists(temp): os.unlink(temp)
    if digest(path.read_bytes()) != digest(raw):
        raise CaptionError("verification_failed", "파일 저장 후 해시가 다릅니다.", path=str(path))


@contextmanager
def locked(state: Path, vid: str):
    folder = state / "locks"; folder.mkdir(parents=True, exist_ok=True)
    with (folder / f"{vid}.lock").open("a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CaptionError("busy", "같은 영상을 다른 실행에서 처리하고 있습니다.")
        try: yield
        finally: fcntl.flock(f, fcntl.LOCK_UN)


def safe_name(text):
    text = unicodedata.normalize("NFC", text)
    text = re.sub(r'[\\/:*?"<>|\[\]#\x00-\x1f]', " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return text.encode()[:120].decode("utf-8", errors="ignore") or "YouTube"


def check_output(output: Path):
    output = output.expanduser().absolute()
    for part in (output, *output.parents):
        if part.is_symlink():
            raise CaptionError("unsafe_path", "심볼릭 링크 폴더에는 저장하지 않습니다.")
    output.mkdir(parents=True, exist_ok=True)
    return output


def note_body(capture):
    m = capture.metadata(); today = now()[:10]
    kind = "자동 생성" if capture.is_generated is True else "수동" if capture.is_generated is False else "미확인"
    if capture.provider == "whisper_medium": kind = "Whisper medium 음성 전사"
    elif capture.provider in {"vision_ocr", "screen_ocr"}: kind = "화면 문자 OCR"
    elif capture.provider == "embedded_subtitle": kind = "내장 자막"
    tags = ["영상대본", "음성전사" if capture.provider == "whisper_medium" else "화면자막OCR" if capture.provider in {"vision_ocr", "screen_ocr"} else "내장자막" if capture.provider == "embedded_subtitle" else "유튜브자막"]
    meta = {"title": capture.title or f"YouTube {capture.video_id} 대본", "aliases": [], "담당": [],
            "구분": ["공통"], "주제": [], "문서유형": "원문", "관련노트": [],
            "원본": [],
            "출처": [m["url"]] + ([capture.source_url] if capture.source_url else []),
            "tags": tags, "created": today, "updated": today, "status": "기록", "분류번호": "00",
            "영상ID": capture.video_id, "자막언어": capture.language or "", "자막종류": kind,
            "수집경로": capture.provider, "수집일": (capture.details.get("source_captured_at") or today)[:10],
            "자료처리": "음성전사완료" if capture.provider == "whisper_medium" else "화면문자추출완료" if capture.provider in {"vision_ocr", "screen_ocr"} else "기존자막추출완료"}
    if capture.published: meta["게시일"] = capture.published
    header = "---\n" + "\n".join(f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in meta.items()) + "\n---\n\n"
    # A fence longer than every source run prevents source text becoming Markdown actions.
    fence = "`" * max(3, max([len(m.group()) for m in re.finditer(r"`+", capture.text)] + [0]) + 1)
    title = safe_name(capture.title or f"YouTube {capture.video_id}")
    body = f"# {title}\n\n출처: {m['url']}\n\n"
    if capture.channel: body += f"채널: {safe_name(capture.channel)}\n\n"
    body += f"자막: {capture.language or '미확인'} · {kind}\n\n"
    body += f"문구가 있는 자막 구간: {sum(bool(r['text'].strip()) for r in capture.segments)}개\n\n"
    body += "검증 범위: 수집한 자막 원본과 추출 본문·시간 구간·저장 파일의 일치. 영상 음성과의 대조는 포함하지 않습니다.\n\n"
    if capture.provider == "whisper_medium":
        body += "음성 인식으로 새로 생성한 대본입니다. 인명·숫자·전문용어와 청취 불명확 구간은 원음 검수가 필요합니다.\n\n"
    elif capture.provider in {"vision_ocr", "screen_ocr"}:
        settings = capture.details["ocr"]
        body += f"화면 문자 인식 결과입니다. 검사 빈도는 초당 {settings['fps']}회, 영역은 {settings['region']}입니다. 표시 시각은 표본 간격만큼 차이 날 수 있고 표본 사이의 짧은 자막은 누락될 수 있습니다. 화면에 없는 발화는 포함하지 않습니다.\n\n"
    elif capture.is_generated is True:
        body += "YouTube 자동 자막의 오인식·반복도 그대로 보존했습니다. 교정하거나 새로 전사한 대본이 아닙니다.\n\n"
    body += f"## 시간별 대본\n\n{fence}text\n{timed_text(capture.segments)}\n{fence}\n"
    return header + body, fence


def write_note(path, content):
    create_file(path, content.encode())
    return {"storage_backend": "local", "index_state": "not_applicable"}


def verify_record(record):
    errors = []
    for name, entry in record.get("files", {}).items():
        p = Path(entry["path"])
        if p.is_symlink() or not p.is_file(): errors.append(name + ":missing"); continue
        if digest(p.read_bytes()) != entry["sha256"]: errors.append(name + ":changed")
    if record.get("schema_version") == 4:
        if set(record.get("files", {})) != {"note"}: errors.append("note:invalid_file_set")
        if not record.get("source_verification", {}).get("ok"): errors.append("source:verification_missing")
        return {"ok": not errors, "errors": errors, "checked_at": now(),
                "scope": "saved_md_hash_after_source_cleanup", "source_alignment": "verified_before_cleanup",
                "audio_alignment": "not_performed", "search_index_visibility": "not_checked"}
    # Hashes are checked in addition to semantic readback, not instead of it.
    try:
        data = json.loads(Path(record["files"]["segments"]["path"]).read_bytes())
        rows = validate_segments(data["segments"])
        text = "\n".join(r["text"] for r in rows)
        if record.get("schema_version", 1) < 3 and Path(record["files"]["text"]["path"]).read_bytes() != text.encode(): errors.append("text:segments_mismatch")
        raw = Path(record["files"]["raw"]["path"]).read_bytes()
        if digest(raw) != data["raw_sha256"]: errors.append("raw:metadata_mismatch")
        if parse(raw, "browser" if data.get("native_browser_export") else data["format"], version=data.get("parser_version", 1)) != rows:
            errors.append("segments:raw_mismatch")
        for evidence in data.get("evidence_files", []):
            path = Path(evidence["path"])
            if path.is_symlink() or not path.is_file() or digest(path.read_bytes()) != evidence["sha256"]:
                errors.append("evidence:changed_or_missing")
        if record.get("schema_version", 1) == 2 and "timed_text" not in record["files"]:
            errors.append("timed_text:missing")
        if "timed_text" in record["files"]:
            if Path(record["files"]["timed_text"]["path"]).read_text() != timed_text(rows): errors.append("timed_text:segments_mismatch")
        note = Path(record["files"]["note"]["path"]).read_text()
        rendered = timed_text(rows) if record.get("schema_version", 1) >= 3 else text
        block = record["fence"] + "text\n" + rendered + "\n" + record["fence"]
        if block not in note: errors.append("note:transcript_mismatch")
        if len(rows) != record["segment_count"]: errors.append("segments:count_mismatch")
        for key in (() if record.get("schema_version", 1) >= 3 else ("raw", "segments", "text") + (("timed_text",) if "timed_text" in record["files"] else ())):
            if Path(record["files"][key]["path"]).name not in note: errors.append(key + ":link_missing")
    except (KeyError, ValueError, OSError, CaptionError):
        errors.append("readback:failed")
    return {"ok": not errors, "errors": errors, "checked_at": now(),
            "scope": "saved_files_and_extraction_source", "audio_alignment": "not_performed",
            "search_index_visibility": "not_checked"}


def staged_capture(folder):
    m = json.loads((folder / "capture.json").read_bytes())
    raw = (folder / ("source." + m["format"])).read_bytes()
    if digest(raw) != m["raw_sha256"]:
        raise CaptionError("verification_failed", "재개 원본의 해시가 달라졌습니다.")
    return Capture(m["video_id"], m["provider"], raw, m["format"], validate_segments(m["segments"]), m["language"],
                   m["is_generated"], m.get("title"), m.get("channel"), m.get("published"),
                   m.get("original_language_evidence"), m["completeness"], m.get("source_url"),
                   {"parser_version": m.get("parser_version", 1),
                    **{k: m[k] for k in DETAIL_KEYS if k in m}})


def rebuild_record(record):
    """Reparse verified source locally; retain the old note/receipt and source bytes."""
    check = verify_record(record)
    if not check["ok"]: raise CaptionError("verification_failed", "재처리할 기존 원본 검증에 실패했습니다.", verification=check)
    m = json.loads(Path(record["files"]["segments"]["path"]).read_bytes())
    raw = Path(record["files"]["raw"]["path"]).read_bytes()
    rows = parse(raw, "browser" if m.get("native_browser_export") else m["format"])
    details = {k: m[k] for k in DETAIL_KEYS if k in m}
    details.update(parser_version=PARSER_VERSION, reprocessed_from=record["receipt"])
    if record.get("captured_at"): details["source_captured_at"] = record["captured_at"]
    return Capture(m["video_id"], m["provider"], raw, m["format"], rows, m["language"], m["is_generated"],
                   m.get("title"), m.get("channel"), m.get("published"), m.get("original_language_evidence"),
                   m["completeness"], m.get("source_url"), details)


def publish(capture, state, output, attempts, writer=write_note):
    output = check_output(output)
    key = digest(json_bytes({"layout_version": 3, "capture": capture.metadata(), "output": str(output)}))[:24]
    folder = state / "captures" / capture.video_id / key
    folder.mkdir(parents=True, exist_ok=True)
    receipt_path = folder / "receipt.json"
    if receipt_path.exists():
        record = json.loads(receipt_path.read_bytes())
        checked = verify_record(record)
        if checked["ok"]:
            if record.get("schema_version") == 4:
                # An explicit refresh may recreate an identical OCR/download job.
                # Reverify this new payload, then let the normal finalizer remove it too.
                raw_path, meta_path = folder / ("source." + capture.format), folder / "capture.json"
                create_file(meta_path, json_bytes(capture.metadata())); create_file(raw_path, capture.raw)
                record.update(schema_version=3, fence=note_body(capture)[1])
                record["files"].update(raw={"path": str(raw_path), "sha256": digest(capture.raw)},
                                       segments={"path": str(meta_path), "sha256": digest(meta_path.read_bytes())})
                checked = verify_record(record)
                if not checked["ok"]: raise CaptionError("verification_failed", "재수집한 원본과 MD가 다릅니다.", verification=checked)
                record["verification"] = checked
                atomic(receipt_path, json_bytes(record))
            return {**record, "status": "already_saved", "verification": checked}
        raise CaptionError("verification_failed", "기존 저장 파일이 변경되었거나 빠졌습니다. 자동으로 덮어쓰지 않습니다.", verification=checked, receipt=str(receipt_path))
    create_file(folder / ("source." + capture.format), capture.raw)
    create_file(folder / "capture.json", json_bytes(capture.metadata()))
    plan_path = folder / "publication.json"
    if plan_path.exists():
        paths = {k: Path(v) for k, v in json.loads(plan_path.read_bytes()).items()}
    else:
        stem = f"{safe_name(capture.title or 'YouTube')} - 대본 ({capture.video_id}, {capture.language or 'und'})"
        for revision in range(1, 10000):
            name = stem + (f" ({revision})" if revision > 1 else "")
            paths = {"note": output / (name + ".md"), "raw": folder / ("source." + capture.format),
                     "segments": folder / "capture.json"}
            if not paths["note"].exists(): break
        else: raise CaptionError("file_conflict", "저장 파일명이 지나치게 많이 충돌합니다.")
        atomic(plan_path, json_bytes({k: str(p) for k, p in paths.items()}))
    atomic(folder / "pending.json", json_bytes({"output": str(output), "attempts": attempts}))
    for name, p in paths.items():
        expected_parent = output if name == "note" else folder
        if p.parent != expected_parent or p.is_symlink():
            raise CaptionError("unsafe_path", "재개 저장 경로가 허용된 위치와 다릅니다.")
    content, fence = note_body(capture)
    try:
        if paths["note"].exists():
            # Recover a crash after note commit but before receipt commit, never rewrite it.
            note = paths["note"].read_text()
            if fence + "text\n" + timed_text(capture.segments) + "\n" + fence not in note:
                raise CaptionError("file_conflict", "재개할 노트가 기존 원문과 다릅니다.")
            saved = {"storage_backend": "recovered_existing_note", "index_state": "pending_shared_service"}
        else:
            saved = writer(paths["note"], content)
    except Exception as exc:
        err = exc if isinstance(exc, CaptionError) else CaptionError("note_write_failed", "노트 저장 중 오류가 발생했습니다.", exception_type=type(exc).__name__)
        return {"status": "partial", "reason": err.kind, "message": err.message, **err.details,
                "video_id": capture.video_id, "capture": str(folder), "resume": True, "attempts": attempts}
    record = {"schema_version": 3, "output_format": "timestamped_markdown", "public_files": ["note"], "parser_version": capture.metadata()["parser_version"],
              "status": "saved", "video_id": capture.video_id,
              "language": capture.language, "is_generated": capture.is_generated, "provider": capture.provider,
              **({"extraction_options": capture.details["extraction_options"]} if "extraction_options" in capture.details else {}),
              "completeness": capture.completeness, "segment_count": len(capture.segments), "fence": fence,
              "output": str(output), "captured_at": capture.details.get("source_captured_at") or now(),
              "processed_at": now(), "attempts": attempts, "receipt": str(receipt_path),
              "files": {k: {"path": str(p), "sha256": digest(p.read_bytes())} for k, p in paths.items()},
              "storage": saved, "parsing": capture.metadata().get("parsing"),
              **({"reprocessed_from": capture.details["reprocessed_from"]} if capture.details.get("reprocessed_from") else {})}
    record["verification"] = verify_record(record)
    if not record["verification"]["ok"]:
        record["status"] = "partial"; record["reason"] = "verification_failed"
        atomic(folder / "verification-failure.json", json_bytes(record))
        return record
    atomic(receipt_path, json_bytes(record))
    cache = state / "videos" / f"{capture.video_id}.json"
    old = json.loads(cache.read_bytes()) if cache.exists() else []
    old = [p for p in old if p != str(receipt_path)] + [str(receipt_path)]
    atomic(cache, json_bytes(old))
    return record


def cached(state, vid, language, output):
    cache = state / "videos" / f"{vid}.json"
    if not cache.exists(): return None
    for entry in reversed(json.loads(cache.read_bytes())):
        record = json.loads(Path(entry).read_bytes())
        actual = (record.get("language") or "").lower()
        match = not language or actual == language.lower() or ("-" not in language and actual.split("-")[0] == language.lower())
        if record.get("output") != str(output) or not match: continue
        check = verify_record(record)
        if not check["ok"]:
            raise CaptionError("verification_failed", "기존 저장본 대조에 실패했습니다. 원본은 덮어쓰지 않았습니다.", receipt=entry, verification=check)
        return {**record, "status": "already_saved", "verification": check}
    return None
