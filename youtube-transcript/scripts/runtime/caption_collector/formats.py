from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse

MAX_BYTES = 32 * 1024 * 1024
PARSER_VERSION = 2
DETAIL_KEYS = ("native_browser_export", "raw_representation", "parser_version", "reprocessed_from", "source_captured_at",
               "source_uri", "local_source", "media_duration", "method", "embedded_stream", "stt", "ocr", "evidence_files", "youtube_media", "extraction_options", "processing_work")
ID = re.compile(r"[A-Za-z0-9_-]{11}\Z")
LANG = re.compile(r"[A-Za-z0-9_-]{1,40}\Z")
TIMESTAMP = r"(?:\d+:)?\d{1,2}:\d{2}(?:[.,]\d+)?"


class CaptionError(Exception):
    def __init__(self, kind, message, **details):
        super().__init__(message)
        self.kind, self.message, self.details = kind, message, details


def video_id(value: str) -> str:
    value = value.strip()
    if ID.fullmatch(value):
        return value
    p = urlparse(value)
    if p.scheme not in ("http", "https") or p.username or p.password or p.port:
        raise CaptionError("invalid_url", "정상적인 유튜브 영상 URL이 필요합니다.")
    host = (p.hostname or "").lower()
    if host in {"youtu.be", "www.youtu.be"}:
        candidate = p.path.strip("/")
    elif host in {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "www.youtube-nocookie.com", "youtube-nocookie.com"}:
        parts = p.path.strip("/").split("/")
        if p.path == "/watch":
            ids = parse_qs(p.query).get("v", [])
            candidate = ids[0] if len(ids) == 1 else ""
        elif len(parts) == 2 and parts[0] in {"shorts", "embed", "live", "v"}:
            candidate = parts[1]
        else:
            candidate = ""
    else:
        candidate = ""
    if not ID.fullmatch(candidate):
        raise CaptionError("invalid_url", "재생목록·채널 주소 대신 영상 하나의 링크를 입력하세요.")
    return candidate


def language_code(value):
    if value is not None and not LANG.fullmatch(value):
        raise CaptionError("invalid_language", "유효한 자막 언어 코드가 필요합니다.")
    return value


def seconds(value):
    parts = value.replace(",", ".").split(":")
    result = 0.0
    for part in parts:
        result = result * 60 + float(part)
    return result


def digest(raw: bytes):
    return hashlib.sha256(raw).hexdigest()


def validate_segments(rows):
    if not isinstance(rows, list) or not rows:
        raise CaptionError("empty_response", "비어 있는 자막은 저장 완료로 처리하지 않습니다.")
    result = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("text"), str):
            raise CaptionError("invalid_caption", "자막 구간의 문구가 올바르지 않습니다.")
        start, duration = row.get("start"), row.get("duration")
        for value in (start, duration):
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0):
                raise CaptionError("invalid_caption", "자막 시간 값이 올바르지 않습니다.")
        if not row["text"]:
            continue
        # Preserve order, whitespace, repeated phrases and markup. Never deduplicate.
        result.append({"text": row["text"], "start": start, "duration": duration})
    if not result or not any(r["text"].strip() for r in result):
        raise CaptionError("empty_response", "자막 본문을 확보하지 못했습니다.")
    starts = [r["start"] for r in result if r["start"] is not None]
    if any(b < a for a, b in zip(starts, starts[1:])):
        raise CaptionError("invalid_caption", "자막 구간의 시간 순서가 뒤섞여 있습니다.")
    return result


def parse(raw: bytes, fmt: str, *, version=PARSER_VERSION):
    if version not in (1, PARSER_VERSION):
        raise CaptionError("unsupported_parser", "지원하지 않는 자막 해석 버전입니다.")
    if len(raw) > MAX_BYTES:
        raise CaptionError("too_large", "32 MiB를 초과한 자막은 자르지 않고 별도 처리를 요청합니다.")
    text = raw.decode("utf-8-sig")
    if fmt in {"json", "json3"}:
        data = json.loads(text)
        if isinstance(data, dict) and "events" in data:
            if not isinstance(data["events"], list):
                raise CaptionError("invalid_caption", "자막 이벤트 목록이 올바르지 않습니다.")
            rows = []
            for event in data["events"]:
                if not isinstance(event, dict):
                    raise CaptionError("invalid_caption", "자막 이벤트를 해석하지 못했습니다.")
                if "segs" not in event:
                    continue
                if version >= 2 and (not isinstance(event["segs"], list) or
                        any(not isinstance(s, dict) or not isinstance(s.get("utf8"), str) for s in event["segs"])):
                    raise CaptionError("invalid_caption", "자막 문구를 빠짐없이 해석하지 못했습니다.")
                content = "".join(s.get("utf8", "") for s in event["segs"])
                # JSON3 append/newline events control the player display; they are
                # not spoken caption cues. The raw source retains every byte.
                if version >= 2 and not content.strip():
                    continue
                if content:
                    if version >= 2:
                        for key in ("tStartMs", "dDurationMs"):
                            value = event.get(key)
                            if key == "dDurationMs" and value is None: continue
                            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                                raise CaptionError("invalid_caption", "자막 시간 값이 없거나 올바르지 않습니다.")
                    rows.append({"text": content, "start": event.get("tStartMs", 0) / 1000,
                                 "duration": event["dDurationMs"] / 1000 if event.get("dDurationMs") is not None else None})
        else:
            rows = data.get("segments", data.get("snippets")) if isinstance(data, dict) else data
        return validate_segments(rows)
    if fmt in {"vtt", "srt"}:
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        rows = []
        cue = re.compile(rf"^({TIMESTAMP})\s+-->\s+({TIMESTAMP})(?:\s+[^\n]*)?$")
        for block in re.split(r"\n[ \t]*\n", normalized):
            lines = block.split("\n")
            if lines and (lines[0].startswith(("WEBVTT", "NOTE", "STYLE", "REGION"))):
                continue
            indices = [i for i, line in enumerate(lines) if "-->" in line]
            if not indices:
                if block.strip():
                    raise CaptionError("invalid_caption", "시간 표시가 없는 자막 블록이 있습니다.")
                continue
            i = indices[0]
            match = cue.fullmatch(lines[i])
            if not match or len(indices) != 1 or i > 1:
                raise CaptionError("invalid_caption", "자막 시간 표시를 전부 해석하지 못했습니다.")
            start, end = map(seconds, match.groups())
            if end < start:
                raise CaptionError("invalid_caption", "종료 시각이 시작 시각보다 빠릅니다.")
            # A final file terminator is syntax; interior line breaks stay intact.
            content = "\n".join(lines[i + 1:]).removesuffix("\n")
            rows.append({"text": content, "start": start, "duration": end - start})
        return validate_segments(rows)
    if fmt in {"txt", "browser"}:
        # Native browser export has a stable header and [m:ss] cue lines.
        if text.startswith("YouTube transcript\n"):
            _, sep, body = text.partition("\n\n")
            if not sep:
                raise CaptionError("invalid_caption", "브라우저 내보내기 본문이 없습니다.")
        else:
            body = text
        marker = re.compile(rf"^\[({TIMESTAMP})\] ?", re.M)
        matches = list(marker.finditer(body))
        if not matches:
            if fmt == "browser":
                raise CaptionError("invalid_caption", "시간 구간이 없는 브라우저 결과입니다.")
            return validate_segments([{"text": body, "start": None, "duration": None}])
        if body[:matches[0].start()].strip():
            raise CaptionError("invalid_caption", "자막 앞에 해석하지 못한 문구가 있습니다.")
        rows = []
        for i, match in enumerate(matches):
            end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
            rows.append({"text": body[match.end():end].removesuffix("\n"),
                         "start": seconds(match.group(1)), "duration": None})
        return validate_segments(rows)
    raise CaptionError("unsupported_format", "SRT·VTT·JSON·JSON3·TXT 자막을 지원합니다.")


def timestamp(value):
    if value is None: return "시각 미제공"
    millis = round(value * 1000)
    hours, millis = divmod(millis, 3600000)
    minutes, millis = divmod(millis, 60000)
    seconds, millis = divmod(millis, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02}.{millis:03}"


def timed_text(rows):
    return "\n\n".join(f"[{timestamp(r['start'])}]\n{r['text']}" for r in rows)


def parsing_audit(raw, fmt):
    if fmt != "json3": return None
    data = json.loads(raw)
    if not isinstance(data, dict) or "events" not in data: return None
    events = data["events"]
    indices = [i for i, e in enumerate(events) if any(s.get("utf8", "").strip() for s in e.get("segs", []))]
    controls = sum(1 for e in events if "segs" in e and not any(s.get("utf8", "").strip() for s in e["segs"]))
    return {"source_event_count": len(events), "text_event_count": len(indices),
            "whitespace_control_event_count": controls, "non_text_event_count": len(events) - len(indices) - controls,
            "source_text_event_indices": indices}


@dataclass
class Capture:
    video_id: str
    provider: str
    raw: bytes
    format: str
    segments: list
    language: str | None
    is_generated: bool | None = None
    title: str | None = None
    channel: str | None = None
    published: str | None = None
    original_language_evidence: str | None = None
    completeness: str = "complete_provider_response"
    source_url: str | None = None
    details: dict = field(default_factory=dict)

    def metadata(self):
        return {"schema_version": 2, "parser_version": self.details.get("parser_version", PARSER_VERSION),
                "parsing": parsing_audit(self.raw, self.format), "video_id": self.video_id,
                "url": self.details.get("source_uri") or f"https://www.youtube.com/watch?v={self.video_id}",
                "provider": self.provider, "format": self.format, "language": self.language,
                "is_generated": self.is_generated, "translation_requested": False,
                "original_language_evidence": self.original_language_evidence,
                "title": self.title, "channel": self.channel, "published": self.published,
                "completeness": self.completeness, "source_url": self.source_url,
                "raw_sha256": digest(self.raw), "segment_count": len(self.segments),
                "segments": self.segments, **self.details}

    @property
    def text(self):
        return "\n".join(row["text"] for row in self.segments)
