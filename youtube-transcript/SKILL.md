---
name: youtube-transcript
description: "유튜브 링크·로컬 영상·음성의 원문 대본을 시간별 Markdown 하나로 추출한다. 기존 자막 우선, 없으면 Whisper medium, 화면 자막은 OCR. 매회 자막 대조·저장 검증·임시 데이터 정리를 확인한다. 새 대본 작성·요약에는 사용하지 않는다."
metadata:
  version: "2.3.1-portable"
  hermes:
    tags: [youtube, transcript, local-media]
---

# 유튜브·로컬 대본 추출

유튜브 URL 또는 로컬 영상·음성을 받아 **시간별 MD 하나**를 저장한다. 기존 자막의 문구·공백·줄바꿈·반복을 그대로 보존하며 요약·교정·번역하지 않는다. Whisper는 새 음성 전사, OCR은 화면 문자 추출로 구분한다. 인용된 링크를 새 추출 요청으로 간주하지 않는다.

이 폴더가 `SKILL_DIR`이다. 설치한 사용자의 컴퓨터에서 독립 실행하며, 별도 API 키·회원 가입·공유 서버가 없다. Python 3.11~3.14와 macOS/Linux 환경이 필요하다. 처음 실행하거나 구성 오류가 나면 [설치 안내](references/install.md)를 읽고 **이 스킬 전용 환경**을 설치한다. 다른 에이전트의 Python 환경에 패키지를 추가하지 않는다.

## 실행

```sh
python3 SKILL_DIR/scripts/collect_transcript.py "https://youtu.be/VIDEO_ID"
python3 SKILL_DIR/scripts/collect_transcript.py "/절대/영상.mp4" --language ko
python3 SKILL_DIR/scripts/collect_transcript.py "/절대/음성.m4a" --mode stt --language ko
python3 SKILL_DIR/scripts/collect_transcript.py "/절대/화면자막.mp4" --mode ocr --ocr-region bottom
```

실제 경로는 인자로 각각 전달하고 공백이 있는 경로는 따옴표로 감싼다. 기본 출력은 **현재 사용자의 `~/Documents/대본MD`**이며 `--output-dir`로 바꿀 수 있다. 처리 상태·재사용 모델·최소 영수증은 `~/.local/share/youtube-transcript`에 둔다. 특정 조직·직원·노트 서비스나 개인 경로에 의존하지 않는다. 일반 Hermes도 같은 위치를 사용한다.

기본 `auto`는 유튜브 기존 자막 → 정상 수집 경로 소진 후 음성 전사, 로컬은 텍스트 내장 자막 → 음성 전사 또는 무음 화면 OCR이다. 음성이 있는 영상의 화면 자막은 `--mode ocr`로 지정한다. `--mode captions`는 기존 자막만 수집한다. 낮은 우선순위, 한 번에 무거운 작업 하나, Whisper medium CPU 2스레드·GPU 미사용, FFmpeg 1스레드, 미디어 다운로드 2MiB/s를 유지한다.

## 완료 확인

JSON 반환값을 확인하고 저장된 MD를 독립 재조회한다. `saved`/`already_saved`, `verification.ok: true`, `completion.ok: true`, `temporary_data_audit.ok: true`, `retention.status: complete`가 모두 필요하다.

- 유튜브 기존 자막은 매회 `yt-dlp` ↔ `youtube-transcript-api`의 다른 경로로 전체 문구·순서·시각·구간 수와 언어·수동/자동 종류를 대조한다. 캐시·재개에도 새로 조회한다. `caption_audit.status: passed`여야 완료다. 이 라이브러리는 구글 YouTube Data API가 아니며 API 키가 필요 없다.
- 로컬·내장 자막·STT·OCR는 유튜브 대조 사본이 없으므로 `not_applicable`과 실제 원본/엔진 결과 대조 범위를 확인한다. 이를 API 대조 성공이라고 보고하지 않는다.
- 차단·요청 제한·불일치는 `partial`이다. 저장된 MD는 유지하고 검증 실패 지점을 알린다. 실패를 숨기기 위해 STT로 바꾸거나 계정·프록시로 우회하지 않는다. 두 자막 수집기는 같은 YouTube 자막을 읽으므로 실제 발화 정확도의 독립 검증은 아니다.
- 저장·원본 대조 후 다운로드 영상/음성, 원시 자막, 전사/OCR 결과, 프레임, 로그, 체크포인트를 자동 삭제하고 실제 잔여를 재확인한다. 사용자 로컬 원본·가져오기 파일은 삭제하지 않는다. MD·재사용 모델/설정·내용 없는 최소 영수증은 유지한다. 검증·저장 실패 시에는 복구 자료가 남을 수 있으므로 완료라고 하지 않는다.
- `partial/reason: cleanup_pending`이면 같은 입력으로 정리를 재개한다. `--verify RECEIPT`는 오프라인 MD 해시 점검이며 새 API 조회가 아니다.

완료 시 **MD 링크 하나**와 추출 방법·대조/정리 결과를 짧게 반환한다. 자동 자막의 오인식, STT 오인식, OCR 표본 사이의 짧은 문구 누락은 저장 검증과 구분한다. 접근·언어 선택·브라우저 가져오기·재개는 [수집과 복구](references/collection.md)를 필요할 때 읽는다. 모든 시청 가능한 영상의 추출을 약속하지 않는다.
