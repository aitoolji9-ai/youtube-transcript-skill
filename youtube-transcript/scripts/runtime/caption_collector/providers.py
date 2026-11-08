from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .formats import CaptionError, Capture, MAX_BYTES, language_code, parse, validate_segments, video_id


def retry_after_seconds(exc):
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or getattr(exc, "headers", {})
    value = headers.get("Retry-After") if headers else None
    if value:
        try:
            return max(1, int(value)), "server_retry_after"
        except (ValueError, TypeError):
            try:
                date = parsedate_to_datetime(value)
                if date.tzinfo is None: date = date.replace(tzinfo=timezone.utc)
                return max(1, int((date - datetime.now(timezone.utc)).total_seconds()) + 1), "server_retry_after"
            except (ValueError, TypeError, OverflowError): pass
    return 60, "local_default"


def classify(exc):
    """Keep observed labels separate from inferred causes; never log signed URLs."""
    if isinstance(exc, CaptionError):
        return exc
    name = type(exc).__name__
    text = str(exc).lower()
    status = getattr(exc, "status", None) or getattr(getattr(exc, "response", None), "status_code", None)
    if status == 429 or "429" in text or "too many requests" in text:
        seconds, basis = retry_after_seconds(exc)
        return CaptionError("rate_limited", "YouTube가 요청 빈도를 제한했습니다.", exception_type=name,
                            retry_after_seconds=seconds, retry_after_basis=basis)
    if name in {"IpBlocked", "RequestBlocked"} or "confirm you’re not a bot" in text or "confirm you're not a bot" in text:
        return CaptionError("request_blocked", "자막 요청이 차단되었습니다. 계정·IP 원인은 별도 확인이 필요합니다.", exception_type=name)
    if any(s in text for s in ("private video", "members-only", "sign in", "login", "age-restricted", "cookies", "keychain")):
        return CaptionError("needs_login", "브라우저에서 정상 시청·로그인 상태를 확인해야 합니다.", exception_type=name)
    if name in {"TranscriptsDisabled", "NoTranscriptFound"} or "no subtitles" in text:
        return CaptionError("no_tracks_reported", "이 경로에서 자막을 찾지 못했습니다.", exception_type=name)
    if name in {"VideoUnavailable", "VideoUnplayable"} or any(s in text for s in ("video unavailable", "video has been removed", "not available in your country")):
        return CaptionError("unavailable", "영상이 삭제되었거나 현재 접근할 수 없습니다.", exception_type=name)
    if status in (401, 403) or "403" in text:
        return CaptionError("access_denied", "해당 자막 요청에 대한 접근이 거부되었습니다.", exception_type=name)
    if any(s in name.lower() + text for s in ("timeout", "timed out", "connectionerror", "connection reset", "502", "503", "504")):
        return CaptionError("transient", "일시적인 통신 오류가 발생했습니다.", exception_type=name)
    return CaptionError("extractor_error", "수집 도구가 응답을 처리하지 못했습니다.", exception_type=name)


def track_options(tracks):
    return [{k: t.get(k) for k in ("language", "is_generated", "original", "name")} for t in tracks]


def select_track(tracks, requested=None, original_hint=None):
    language_code(requested)
    if not tracks:
        raise CaptionError("no_tracks_reported", "이 경로에 번역을 요청하지 않은 자막 트랙이 없습니다.")
    available = tracks
    evidence = None
    family = lambda language: language.lower().split("-")[0]
    if requested:
        available = [t for t in tracks if t["language"].lower() == requested.lower()]
        # A generic BCP-47 language includes its regional variants. A specifically
        # requested region stays exact and is never silently replaced.
        if "-" not in requested:
            available = [t for t in tracks if family(t["language"]) == requested.lower()]
        evidence = "user_selected_language"
        if not available:
            raise CaptionError("needs_language", "요청한 언어의 자막을 찾지 못했습니다.", tracks=track_options(tracks))
    else:
        originals = {t["language"] for t in tracks if t.get("original")}
        if len({family(lang) for lang in originals}) == 1:
            lang = next(iter(originals)); evidence = "provider_original_track"
            available = [t for t in tracks if family(t["language"]) == family(lang)]
        elif original_hint and len({family(t["language"]) for t in tracks}) > 1 and any(t["language"] == original_hint for t in tracks):
            # A hint is recorded, never promoted to verified original provenance.
            raise CaptionError("needs_language", "여러 자막 중 원어 트랙을 확인하려면 언어 선택이 필요합니다.", tracks=track_options(tracks), suggested_language=original_hint)
        elif len({family(t["language"]) for t in tracks}) != 1:
            raise CaptionError("needs_language", "원어를 확정할 수 없어 자막 언어 선택이 필요합니다.", tracks=track_options(tracks))
        else:
            evidence = "sole_available_language_original_unverified"
    available = sorted(available, key=lambda t: (t.get("is_generated") is not False, not t.get("original", False)))
    return available[0], evidence


class QuietLogger:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


def ytdlp_capture(vid, requested=None, browser=None, profile=None):
    import yt_dlp
    opts = {"quiet": True, "no_warnings": True, "logger": QuietLogger(), "noplaylist": True,
            "skip_download": True, "cachedir": False, "socket_timeout": 20,
            "retries": 0, "extractor_retries": 0, "ignore_no_formats_error": True}
    if browser:
        if browser not in {"chrome", "edge", "firefox", "safari", "brave", "chromium", "whale", "vivaldi", "opera"}:
            raise CaptionError("invalid_browser", "지원되는 브라우저 이름이 필요합니다.")
        opts["cookiesfrombrowser"] = (browser, profile)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False)
        if not info or info.get("id") != vid:
            raise CaptionError("wrong_video", "응답의 영상 ID가 요청과 다릅니다.")
        if info.get("is_live"):
            raise CaptionError("live_in_progress", "진행 중인 라이브는 전체 자막으로 저장하지 않습니다. 종료 후 다시보기를 이용하세요.")
        tracks = []
        for field, generated in (("subtitles", False), ("automatic_captions", True)):
            for code, formats in (info.get(field) or {}).items():
                usable = []
                for f in formats:
                    p = urlparse(f.get("url", "")); q = parse_qs(p.query)
                    if p.scheme != "https" or not ((p.hostname or "").endswith(".youtube.com") or p.hostname in {"youtube.com", "video.google.com"}):
                        continue
                    if q.get("tlang") or f.get("ext") not in {"json3", "vtt", "srt"}:
                        continue
                    usable.append(f)
                if not usable or code == "live_chat":
                    continue
                lang = code.removesuffix("-orig")
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", lang):
                    continue
                tracks.append({"language": lang, "name": usable[0].get("name", code),
                               "is_generated": generated, "original": code.endswith("-orig"), "formats": usable})
        track, evidence = select_track(tracks, requested, info.get("language"))
        failures = []
        for f in sorted(track["formats"], key=lambda f: {"json3": 0, "vtt": 1, "srt": 2}[f["ext"]]):
            try:
                with ydl.urlopen(f["url"]) as response:
                    raw = response.read(MAX_BYTES + 1)
                rows = parse(raw, f["ext"])
                return Capture(vid, "yt_dlp_authenticated" if browser else "yt_dlp", raw, f["ext"], rows,
                               track["language"], track["is_generated"], info.get("title"), info.get("channel"),
                               info.get("upload_date"), evidence)
            except Exception as exc:
                err = classify(exc); failures.append(err.kind)
                if err.kind in {"rate_limited", "request_blocked", "access_denied", "needs_login"}:
                    raise err
        raise CaptionError("caption_fetch_failed", "자막 트랙은 있으나 본문을 확보하지 못했습니다.", format_failures=failures)


def api_capture(vid, requested=None):
    from requests import Session
    from youtube_transcript_api import YouTubeTranscriptApi

    class BoundedSession(Session):
        def request(self, *args, **kwargs):
            kwargs.setdefault("timeout", 20)
            return super().request(*args, **kwargs)

    with BoundedSession() as session:
        api = YouTubeTranscriptApi(http_client=session)
        listing = list(api.list(vid))
        tracks = [{"language": t.language_code, "name": t.language, "is_generated": t.is_generated,
                   "original": False, "object": t} for t in listing]
        # Auto-generated is a caption type, not proof of the video's original language.
        generated_languages = {t["language"] for t in tracks if t["is_generated"]}
        hint = next(iter(generated_languages)) if len(generated_languages) == 1 else None
        track, evidence = select_track(tracks, requested, hint)
        fetched = track["object"].fetch(preserve_formatting=True)
        rows = validate_segments(fetched.to_raw_data())
        raw = json.dumps({"video_id": vid, "language": track["language"], "is_generated": track["is_generated"],
                          "segments": rows}, ensure_ascii=False, indent=2, allow_nan=False).encode()
        return Capture(vid, "youtube_transcript_api", raw, "json", rows, track["language"],
                       track["is_generated"], original_language_evidence=evidence,
                       details={"raw_representation": "library_returned_segments"})


def import_capture(vid, path, requested=None, browser_export=False, metadata=None):
    metadata = metadata or {}
    p = Path(path)
    if not p.is_file() or p.is_symlink() or p.stat().st_size > MAX_BYTES:
        raise CaptionError("invalid_file", "읽을 수 있는 32 MiB 이하 자막 파일이 필요합니다.")
    raw = p.read_bytes()
    fmt = "browser" if browser_export else p.suffix.lower().lstrip(".")
    embedded = {}
    if browser_export:
        text = raw.decode("utf-8-sig")
        if not text.startswith("YouTube transcript\n"):
            raise CaptionError("invalid_browser_export", "브라우저 전용 내보내기 파일의 헤더가 없습니다.")
        for line in text.partition("\n\n")[0].splitlines()[1:]:
            key, sep, value = line.partition(": ")
            if sep: embedded[key] = value
        if embedded.get("Video ID") != vid:
            raise CaptionError("wrong_video", "브라우저 내보내기 영상 ID가 요청과 다릅니다.")
        lang = embedded.get("Language")
        if requested and not (lang == requested or ("-" not in requested and (lang or "").split("-")[0] == requested)):
            raise CaptionError("needs_language", "내보낸 자막 언어가 요청과 다릅니다.", exported_language=lang)
        requested = lang
    elif fmt in {"json", "json3"}:
        data = json.loads(raw)
        if isinstance(data, dict):
            if data.get("video_id") and data["video_id"] != vid:
                raise CaptionError("wrong_video", "자막 파일의 영상 ID가 요청과 다릅니다.")
            lang = data.get("language_code") or data.get("language")
            if requested and lang and lang != requested:
                raise CaptionError("needs_language", "자막 파일 언어가 요청과 다릅니다.")
            requested = requested or lang
    language_code(requested)
    if not requested:
        raise CaptionError("needs_language", "제공된 자막 파일의 언어를 --language로 지정하세요.")
    label = embedded.get("Captions", "").lower()
    generated = True if label in {"auto-generated", "automatically generated", "generated"} else None
    if metadata.get("is_generated") in (True, False) and isinstance(metadata.get("is_generated"), bool):
        generated = metadata["is_generated"]
    rows = parse(raw, fmt)
    return Capture(vid, "browser_export" if browser_export else "provided_file", raw,
                   "txt" if browser_export else fmt, rows, requested, generated,
                   metadata.get("title"), metadata.get("channel"), metadata.get("published"),
                   "user_selected_language" if not browser_export else None,
                   "complete_exported_file" if browser_export else "complete_provided_file",
                   metadata.get("source_url"), details={"native_browser_export": browser_export})


def collect(vid, requested=None, provider="auto", browser=None, profile=None, runners=None):
    runners = runners or {"yt_dlp": ytdlp_capture, "youtube_transcript_api": api_capture}
    order = ["yt_dlp", "youtube_transcript_api"] if provider == "auto" else [provider]
    if browser:
        order = ["yt_dlp"]  # Auth is only used on explicit continuation after browser review.
    attempts, language_requests = [], []
    for name in order:
        for attempt in range(2):
            try:
                args = (vid, requested, browser, profile) if name == "yt_dlp" else (vid, requested)
                capture = runners[name](*args)
                attempts.append({"provider": capture.provider, "outcome": "captured"})
                return capture, attempts, None
            except Exception as exc:
                err = classify(exc)
                attempts.append({"provider": name, "outcome": err.kind, **err.details})
                if err.kind == "needs_language": language_requests.append(err.details)
                if err.kind == "transient" and attempt == 0:
                    time.sleep(2); continue
                if err.kind in {"rate_limited", "request_blocked", "access_denied", "live_in_progress", "needs_login"}:
                    # No tool hopping to evade an explicit restriction.
                    return None, attempts, {"status": "needs_login" if err.kind == "needs_login" else "needs_browser",
                                            "reason": err.kind, "message": err.message, **err.details}
                break
    if language_requests:
        return None, attempts, {"status": "needs_language", "reason": "ambiguous_language", "choices": language_requests}
    return None, attempts, {"status": "needs_browser", "reason": "automatic_routes_exhausted",
                           "message": "브라우저의 스크립트 표시와 내보내기로 이어가세요. 자막 없음으로 확정하지 않았습니다."}
