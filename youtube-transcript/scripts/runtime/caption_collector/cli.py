from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import time
from pathlib import Path

from .formats import DETAIL_KEYS, PARSER_VERSION, CaptionError, Capture, language_code, parse, video_id
from .providers import api_capture, classify, collect, import_capture, ytdlp_capture
from .storage import LOCAL_OUTPUT, PROJECT, STATE, atomic, cached, check_output, json_bytes, locked, publish, rebuild_record, staged_capture, verify_record
from .completion import complete


def worker(payload):
    try:
        fn = ytdlp_capture if payload["provider"] == "yt_dlp" else api_capture
        args = [payload["video_id"], payload.get("language")]
        if payload["provider"] == "yt_dlp": args += [payload.get("browser"), payload.get("profile")]
        c = fn(*args)
        return {"capture": c.metadata(), "raw_base64": base64.b64encode(c.raw).decode()}
    except Exception as exc:
        err = classify(exc)
        return {"error": {"kind": err.kind, "message": err.message, "details": err.details}}


def isolated(provider, vid, language=None, browser=None, profile=None):
    payload = {"provider": provider, "video_id": vid, "language": language, "browser": browser, "profile": profile}
    try:
        proc = subprocess.run([sys.executable, "-m", "caption_collector", "--worker"], cwd=PROJECT,
                              input=json.dumps(payload), capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired:
        raise CaptionError("provider_timeout", "수집 경로의 90초 제한에 도달했습니다. 확보된 결과 없이 중단했습니다.")
    if proc.returncode:
        raise CaptionError("provider_error", "수집 프로세스가 실패했습니다.", returncode=proc.returncode)
    data = json.loads(proc.stdout)
    if "error" in data:
        e = data["error"]; raise CaptionError(e["kind"], e["message"], **e["details"])
    m = data["capture"]
    c = Capture(m["video_id"], m["provider"], base64.b64decode(data["raw_base64"], validate=True),
                m["format"], m["segments"], m["language"], m["is_generated"], m.get("title"), m.get("channel"),
                m.get("published"), m.get("original_language_evidence"), m["completeness"], m.get("source_url"),
                {k: m[k] for k in DETAIL_KEYS if k in m})
    return c


def handoff(result, vid, state):
    result["video_id"] = vid
    result["url"] = f"https://www.youtube.com/watch?v={vid}"
    if result.get("reason") == "rate_limited":
        seconds = result.get("retry_after_seconds", 60)
        cooldown = {"retry_not_before": time.time() + seconds, "retry_after_basis": result.get("retry_after_basis", "local_default")}
        atomic(state / "youtube-cooldown.json", json_bytes(cooldown))
        result.update(cooldown)
    if result["status"] in {"needs_browser", "needs_login"}:
        result["next_action"] = {
            "tool": "current_agent_browser",
            "steps": ["Open the canonical URL in the user's authorized browser; inspect current access and language.",
                      "Open Show transcript. Do not label a loading/blocked panel as no captions.",
                      "Codex: use tab.content.exportYouTubeTranscript() and pass its returned local path to --browser-export.",
                      "Hermes: use its supported browser transcript export when available; otherwise preserve the full timestamped DOM text with an observed end boundary as an import.",
                      "Use --browser only after normal playback access is confirmed and browser export is unavailable; never bypass a denied permission or rate limit."],
            "resume_command": [str(PROJECT / "run"), result["url"], "--browser-export", "<exported-file.txt>"],
            "no_caption_policy": "An empty API result alone is not evidence that the video has no captions."}
    atomic(state / "requests" / f"{vid}.json", json_bytes(result))
    return result


def parser():
    p = argparse.ArgumentParser(description="유튜브·로컬 대본을 추출하여 문서/대본MD에 시간별 MD로 저장합니다.")
    p.add_argument("url", nargs="?")
    p.add_argument("--language")
    p.add_argument("--provider", choices=["auto", "yt_dlp", "youtube_transcript_api"], default="auto")
    p.add_argument("--mode", choices=["auto", "captions", "stt", "ocr"], default="auto",
                   help="기존 자막 우선 자동 처리, 자막만, 음성 전사 또는 화면 OCR")
    p.add_argument("--ocr-fps", type=float, default=2.0)
    p.add_argument("--ocr-region", default="full", help="full, bottom 또는 x,y,w,h 비율")
    sources = p.add_mutually_exclusive_group()
    sources.add_argument("--import-file", type=Path)
    sources.add_argument("--browser-export", type=Path)
    p.add_argument("--metadata", type=Path, help="확인된 title/channel/published/source_url/is_generated JSON")
    p.add_argument("--title")
    p.add_argument("--channel")
    p.add_argument("--output-dir", type=Path, default=LOCAL_OUTPUT,
                   help="기본: ~/Documents/대본MD; --output-dir로 변경 가능")
    p.add_argument("--state-dir", type=Path, default=STATE)
    p.add_argument("--refresh", action="store_true", help="이전 파일을 보존하며 새 자막 수집")
    p.add_argument("--resume", action="store_true", help="보존 원본으로 저장 재개; 필수 자막 대조는 네트워크 조회")
    p.add_argument("--browser", help="브라우저 시청권한 확인 후 인증된 yt-dlp를 명시적으로 사용")
    p.add_argument("--browser-profile")
    p.add_argument("--browser-access-confirmed", action="store_true")
    p.add_argument("--verify", type=Path, metavar="RECEIPT")
    p.add_argument("--json", action="store_true", help="결과는 항상 JSON입니다")
    return p


def finish(record, state):
    return complete(record, state, isolated)


def mode_matches(record, mode, fps=2.0, region="full"):
    if record.get("provider") in {"vision_ocr", "screen_ocr"}:
        options = record.get("extraction_options", {"fps": 2.0, "region": "full"})
        if options.get("fps") != fps or options.get("region") != region: return False
    if mode == "auto": return True
    if mode == "stt": return record.get("provider") == "whisper_medium"
    if mode == "ocr": return record.get("provider") in {"vision_ocr", "screen_ocr"}
    return record.get("provider") not in {"whisper_medium", "vision_ocr", "screen_ocr"}


def local_run(args):
    from .media import local_capture, source_info
    from .resources import media_slot
    state = args.state_dir.expanduser().absolute(); output = check_output(args.output_dir)
    with media_slot():
        path, source = source_info(args.url)
        vid = "local-" + source["sha256"][:20]
        with locked(state, vid):
            if not args.refresh:
                existing = cached(state, vid, args.language, output)
                if existing and mode_matches(existing, args.mode, args.ocr_fps, args.ocr_region): return finish(existing, state)
            capture, attempts = local_capture(path, state, method=args.mode, language=args.language,
                                              identity=vid, fps=args.ocr_fps, region=args.ocr_region, source_record=source)
            if args.title: capture.title = args.title
            return finish(publish(capture, state, output, attempts), state)


def youtube_media(args, vid, state, output, attempts):
    from .media import download_youtube, local_capture
    from .providers import classify
    from .resources import media_slot
    with media_slot():
        try:
            path, info = download_youtube(vid, state, video=args.mode == "ocr",
                                          browser=args.browser, profile=args.browser_profile)
            capture, extra = local_capture(path, state, identity=vid,
                                           method="ocr" if args.mode == "ocr" else "auto" if args.mode == "auto" else "stt",
                                           language=args.language or info.get("language"),
                                           fps=args.ocr_fps, region=args.ocr_region)
        except Exception as exc:
            error = classify(exc)
            if error.kind == "rate_limited":
                return handoff({"status": "needs_browser", "reason": error.kind, "message": error.message,
                                **error.details, "attempts": attempts}, vid, state)
            raise error
        capture.title = args.title or info.get("title") or capture.title
        capture.channel = args.channel or info.get("channel")
        capture.details["source_uri"] = f"https://www.youtube.com/watch?v={vid}"
        capture.details["youtube_media"] = {"sha256": info["sha256"], "duration": info.get("duration"),
                                            "network_limit_bytes_per_second": info["network_limit_bytes_per_second"]}
        return finish(publish(capture, state, output, attempts + extra), state)


def can_fallback_to_media(result, attempts):
    denied = {"rate_limited", "request_blocked", "access_denied", "needs_login", "live_in_progress", "unavailable", "needs_language"}
    return (result.get("reason") == "automatic_routes_exhausted" and
            not any(a.get("outcome") in denied for a in attempts))


def run(args):
    if args.verify:
        record = json.loads(args.verify.read_bytes())
        check = verify_record(record)
        return {"status": "verified" if check["ok"] else "verification_failed", "verification": check,
                "video_id": record["video_id"], "files": record["files"],
                **{k: record[k] for k in ("caption_audit", "temporary_data_audit", "completion") if k in record}}
    if not args.url: raise CaptionError("invalid_url", "유튜브 영상 URL이 필요합니다.")
    if not args.url.startswith(("https://", "http://")) and (Path(args.url).expanduser().exists() or len(args.url) != 11):
        return local_run(args)
    vid = video_id(args.url); language_code(args.language)
    if args.browser and not args.browser_access_confirmed:
        raise CaptionError("needs_login", "먼저 브라우저에서 해당 영상의 정상 시청 권한을 확인하세요.")
    state = args.state_dir.expanduser().absolute(); output = check_output(args.output_dir)
    with locked(state, vid):
        if args.resume:
            existing = cached(state, vid, args.language, output)
            if existing and existing.get("schema_version") == 4:
                return finish(existing, state)
            root = state / "captures" / vid
            for pending in sorted(root.glob("*/pending.json"), key=lambda p: p.stat().st_mtime, reverse=True):
                m = json.loads(pending.read_bytes()); c = staged_capture(pending.parent)
                match = not args.language or c.language == args.language or ("-" not in args.language and (c.language or "").split("-")[0] == args.language)
                if m["output"] == str(output) and match:
                    if c.details.get("parser_version", 1) < PARSER_VERSION:
                        c.segments = parse(c.raw, "browser" if c.details.get("native_browser_export") else c.format)
                        c.details.update(parser_version=PARSER_VERSION, reprocessed_from=str(pending.parent))
                    return finish(publish(c, state, output, m["attempts"]), state)
            raise CaptionError("no_pending_capture", "재개할 원본이 없습니다. 새 수집 명령을 사용하세요.")
        supplied = args.browser_export or args.import_file
        if not args.refresh and not supplied:
            existing = cached(state, vid, args.language, output)
            if existing and mode_matches(existing, args.mode, args.ocr_fps, args.ocr_region):
                if existing.get("schema_version") != 4 and (existing.get("parser_version", 1) < PARSER_VERSION or existing.get("schema_version", 1) < 3):
                    capture = rebuild_record(existing)
                    return finish(publish(capture, state, output, existing.get("attempts", []) +
                                   [{"provider": "local_source_reprocess", "outcome": "reparsed_without_network"}]), state)
                return finish(existing, state)
        if supplied:
            meta = json.loads(args.metadata.read_bytes()) if args.metadata else {}
            if not isinstance(meta, dict): raise CaptionError("invalid_metadata", "메타데이터는 JSON 객체여야 합니다.")
            if args.title: meta["title"] = args.title
            if args.channel: meta["channel"] = args.channel
            capture = import_capture(vid, supplied, args.language, bool(args.browser_export), meta)
            attempts = [{"provider": capture.provider, "outcome": "captured"}]
        else:
            cooldown_path = state / "youtube-cooldown.json"
            if cooldown_path.exists():
                cooldown = json.loads(cooldown_path.read_bytes())
                if cooldown["retry_not_before"] > time.time():
                    return {"status": "rate_limited", "video_id": vid, **cooldown,
                            "message": "이전 요청의 대기 시간이 남아 있어 네트워크를 재호출하지 않았습니다. 확보한 파일 가져오기와 저장 재개는 가능합니다."}
            runners = {"yt_dlp": lambda v, l, b=None, p=None: isolated("yt_dlp", v, l, b, p),
                       "youtube_transcript_api": lambda v, l: isolated("youtube_transcript_api", v, l)}
            if args.mode in {"stt", "ocr"}:
                return youtube_media(args, vid, state, output, [])
            capture, attempts, result = collect(vid, args.language, args.provider, args.browser, args.browser_profile, runners)
            if result:
                if args.mode == "auto" and can_fallback_to_media(result, attempts):
                    return youtube_media(args, vid, state, output, attempts)
                return handoff({**result, "attempts": attempts}, vid, state)
            if args.title: capture.title = args.title
            if args.channel: capture.channel = args.channel
        return finish(publish(capture, state, output, attempts), state)


def main():
    if sys.argv[1:] == ["--worker"]:
        print(json.dumps(worker(json.load(sys.stdin)), ensure_ascii=False, allow_nan=False)); return
    args = parser().parse_args()
    try:
        result = run(args)
    except CaptionError as exc:
        result = {"status": exc.kind, "message": exc.message, **exc.details}
    except (ValueError, OSError, KeyError, TypeError) as exc:
        result = {"status": "failed", "reason": type(exc).__name__, "message": "입력 또는 저장 상태를 읽지 못했습니다. 원본은 덮어쓰지 않았습니다."}
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    if result["status"] not in {"saved", "already_saved", "verified"}: sys.exit(2)
