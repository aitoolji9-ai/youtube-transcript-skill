"""Remove successful jobs' payloads; retain only MD and compact integrity receipts."""
from __future__ import annotations

import json
from pathlib import Path
import re
import shutil

from .formats import CaptionError, digest, timed_text
from .storage import STATE, atomic, json_bytes, now, verify_record


def owned(path, root):
    path, root = Path(path).absolute(), Path(root).absolute()
    if path == root or not path.is_relative_to(root):
        raise CaptionError("unsafe_cleanup", "자동 정리 범위를 벗어났습니다.")
    for part in (path, *path.parents):
        if part.is_symlink(): raise CaptionError("unsafe_cleanup", "심볼릭 링크는 자동 정리하지 않습니다.")
        if part == root: break
    return path


def prepare(record, state):
    """Commit a source-verified receipt before deleting anything (crash resumable)."""
    compact = record.get("schema_version") == 4
    recovered_meta = Path(record["receipt"]).parent / "capture.json"
    if compact and (record.get("retention", {}).get("status") == "pending" or not recovered_meta.exists()): return record
    check = verify_record(record)
    if not check["ok"]:
        raise CaptionError("verification_failed", "저장본 검증에 실패하여 처리 자료를 삭제하지 않았습니다.", verification=check)
    state = Path(state).absolute()
    vid = record["video_id"]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", vid): raise CaptionError("unsafe_cleanup", "정리할 영상 ID가 올바르지 않습니다.")
    receipt = owned(record["receipt"], state / "captures" / vid)
    if receipt.name != "receipt.json" or receipt.parent.parent != state / "captures" / vid:
        raise CaptionError("unsafe_cleanup", "정리 영수증의 위치가 올바르지 않습니다.")
    meta = json.loads((recovered_meta if compact else Path(record["files"]["segments"]["path"])).read_bytes())
    if compact:
        if meta["raw_sha256"] != record["source_verification"]["raw_sha256"] or timed_text(meta["segments"]) not in Path(record["files"]["note"]["path"]).read_text():
            raise CaptionError("verification_failed", "중단된 재수집 자료가 기존 MD와 다릅니다.")
    targets = []
    # Capture payloads share a folder with the tiny receipt. Never remove the receipt itself.
    targets.extend({"path": str(p), "root": str(receipt.parent)} for p in receipt.parent.iterdir() if p != receipt)
    work = meta.get("processing_work")
    if work:
        targets.append({"path": str(owned(work, state / "media")), "root": str(state / "media")})
    else:
        # Pre-policy jobs: derive known work keys, and match evidence checkpoints by source hash.
        source_sha = meta.get("local_source", {}).get("sha256")
        for method in ("auto", "captions", "stt", "ocr"):
            values = {"source": source_sha, "method": method, "language": meta.get("language"),
                      "fps": meta.get("ocr", {}).get("fps", 2.0), "region": meta.get("ocr", {}).get("region", "full")}
            for version in (False, True):
                key = dict(values)
                if version: key["ocr_algorithm"] = 3
                p = state / "media" / digest(json_bytes(key))[:24]
                if p.exists(): targets.append({"path": str(p), "root": str(state / "media")})
        if source_sha:
            for p in (state / "media").glob("*/stt.json"):
                if p.is_symlink(): continue
                saved = json.loads(p.read_bytes())
                if saved.get("signature", {}).get("source_sha256") == source_sha:
                    targets.append({"path": str(p.parent), "root": str(state / "media")})
    for entry in meta.get("evidence_files", []):
        path = Path(entry["path"])
        if path.is_relative_to(STATE / "stt/jobs"):
            job = owned(path.parent, STATE / "stt/jobs")
            if job.parent != STATE / "stt/jobs": raise CaptionError("unsafe_cleanup", "전사 작업 위치가 올바르지 않습니다.")
            targets.append({"path": str(job), "root": str(STATE / "stt/jobs")})
        elif path.is_relative_to(state / "media"):
            targets.append({"path": str(owned(path.parent, state / "media")), "root": str(state / "media")})
    for path, root in [(state / "downloads" / vid, state / "downloads"), (state / "requests" / (vid + ".json"), state / "requests")]:
        if path.exists(): targets.append({"path": str(owned(path, root)), "root": str(root)})
    # Legacy published sidecars are generated copies, never the supplied import/local original.
    for name, entry in record["files"].items():
        path = Path(entry["path"])
        if name != "note" and path.parent == Path(record["output"]):
            targets.append({"path": str(owned(path, Path(record["output"]))), "root": str(Path(record["output"])), "expected_sha256": entry["sha256"]})
    unique = list({t["path"]: t for t in targets}.values())
    note = Path(record["files"]["note"]["path"])
    for target in unique:
        path = owned(target["path"], target["root"])
        if note == path or note.is_relative_to(path): raise CaptionError("unsafe_cleanup", "완성 대본은 자동 정리 대상이 아닙니다.")
    keep = {k: record[k] for k in ("status", "video_id", "language", "provider", "is_generated", "segment_count", "output", "captured_at", "processed_at", "receipt", "extraction_options", "completeness", "parser_version", "reprocessed_from", "caption_audit", "temporary_data_audit") if k in record}
    keep.update(schema_version=4, output_format="timestamped_markdown", public_files=["note"],
                files={"note": record["files"]["note"]},
                source_verification=record["source_verification"] if compact else {"ok": True, "checked_at": check["checked_at"], "scope": check["scope"], "raw_sha256": record["files"]["raw"]["sha256"]},
                retention={"policy": "md_and_integrity_receipt_only", "status": "pending", "targets": unique})
    keep["verification"] = verify_record(keep)
    atomic(receipt, json_bytes(keep))
    return keep


def erase_pending(record):
    # Recheck MD immediately before deletion, including crash recovery.
    check = verify_record(record)
    if not check["ok"]:
        raise CaptionError("verification_failed", "MD 변경을 발견해 삭제를 중단했습니다.", verification=check)
    record["verification"] = check
    receipt = Path(record["receipt"])
    if receipt.parents[2].name == "captures":
        state = receipt.parents[3]
        allowed = {receipt.parent, state / "media", state / "downloads", state / "requests", STATE / "stt/jobs"}
        output = Path(record["output"])
    elif receipt.parent.parent == STATE / "stt/jobs":
        allowed, output = {receipt.parent}, None
    else: raise CaptionError("unsafe_cleanup", "정리 영수증의 위치가 올바르지 않습니다.")
    targets = list(record["retention"].get("targets", record["retention"].get("managed_targets", [])))
    if record["retention"].get("status") == "complete" and receipt.parents[2].name == "captures":
        targets += [{"path": str(p), "root": str(receipt.parent)} for p in receipt.parent.iterdir() if p != receipt]
    for target in targets:
        root = Path(target["root"])
        if root not in allowed and not (root == output and target.get("expected_sha256")):
            raise CaptionError("unsafe_cleanup", "허용되지 않은 정리 위치입니다.")
        path = owned(target["path"], target["root"])
        note = Path(record["files"]["note"]["path"])
        if path == receipt or path == note or note.is_relative_to(path):
            raise CaptionError("unsafe_cleanup", "완성 대본과 검증 영수증은 삭제하지 않습니다.")
        if target.get("expected_sha256") and path.exists() and (not path.is_file() or digest(path.read_bytes()) != target["expected_sha256"]):
            raise CaptionError("verification_failed", "기존 부속 파일 변경을 발견해 삭제를 중단했습니다.")
        if path.is_dir(): shutil.rmtree(path)
        else: path.unlink(missing_ok=True)
    record["retention"] = {"policy": "md_and_integrity_receipt_only", "status": "complete", "cleaned_at": now(),
                           "managed_targets": [t for t in targets if Path(t["root"]) != receipt.parent]}
    record["verification"] = verify_record(record)
    atomic(Path(record["receipt"]), json_bytes(record))
    return record


def finalize(record, state):
    if record.get("status") not in {"saved", "already_saved"}: return record
    try:
        # Prepare all verified editions of this video before removing shared engine evidence.
        records = []
        for p in (Path(state) / "captures" / record["video_id"]).glob("*/receipt.json"):
            other = json.loads(p.read_bytes())
            if verify_record(other)["ok"]:
                records.append(prepare(other, state))
        current = next((r for r in records if r["receipt"] == record["receipt"]), None)
        if current is None: current = prepare(record, state); records.append(current)
        for item in records: erase_pending(item)
        current["status"] = record["status"]
        return current
    except (OSError, ValueError, KeyError, CaptionError) as exc:
        return {"status": "partial", "reason": "cleanup_pending", "message": "MD는 저장됐지만 처리 자료 정리가 끝나지 않았습니다. 같은 요청을 다시 실행하면 정리를 재개합니다.",
                "video_id": record["video_id"], "files": {"note": record["files"]["note"]},
                "receipt": record["receipt"], "cleanup_error": getattr(exc, "kind", type(exc).__name__)}
