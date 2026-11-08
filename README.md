# 유튜브·로컬 대본 추출 스킬

유튜브 링크·로컬 영상·음성에서 **시간별 Markdown 하나**를 추출합니다. 기존 자막을 우선 보존하고, 없으면 Whisper medium으로 전사하며, 화면에 박힌 자막은 OCR로 읽습니다. 각자의 컴퓨터에서 독립적으로 실행합니다. API 키·회원 가입·공유 서버가 필요 없습니다.

[최신 ZIP 다운로드](https://github.com/aitoolji9-ai/youtube-transcript-skill/releases/latest) · [상세 설치 안내](youtube-transcript/references/install.md) · [실제 검증 결과](evidence/verification.json)

## 설치

macOS, Python **3.11~3.14**가 필요합니다. 전체 기능을 설치하려면 [Homebrew](https://brew.sh/)도 준비합니다. Linux/WSL은 실행 경로와 자동 테스트를 제공하지만 macOS와 같은 실제 미디어 종단 검증 범위는 아닙니다. Windows 기본 Python은 지원하지 않습니다.

ZIP을 풀고 `youtube-transcript` 폴더에서 설치할 도구를 선택합니다. `python3.14`는 설치한 호환 Python 명령으로 바꿀 수 있습니다.

```sh
python3.14 scripts/install_skill.py codex
# Hermes라면 대신:
python3.14 scripts/install_skill.py hermes
```

설치기는 기존 스킬을 덮어쓰지 않습니다. **복사된 스킬 폴더**로 이동해 필요한 구성을 설치합니다. Codex의 예:

```sh
cd ~/.codex/skills/youtube-transcript
python3.14 scripts/setup.py --media --install-tools
python3.14 scripts/setup.py --check --media
```

Hermes는 `~/.hermes/skills/youtube-transcript`로 이동합니다. 자막만 필요하면 `python3.14 scripts/setup.py`로 설치할 수 있습니다. `captions_ready: true`는 자막 경로, `media_ready: true`는 로컬/STT/OCR 구성 점검 통과입니다. Python 의존성은 스킬 전용 가상 환경에 설치하며, 운영 중인 에이전트의 Python 환경을 수정하지 않습니다.

## 사용

```sh
python3 scripts/collect_transcript.py "https://youtu.be/VIDEO_ID"
python3 scripts/collect_transcript.py "/절대/영상.mp4" --language ko
python3 scripts/collect_transcript.py "/절대/음성.m4a" --mode stt --language ko
python3 scripts/collect_transcript.py "/절대/화면자막.mp4" --mode ocr --ocr-region bottom
```

기본 저장 위치는 **자신의 `~/Documents/대본MD`**입니다. `--output-dir`로 변경할 수 있습니다. 스킬 새로고침/다음 세션에서 목록에 등록됐는지 확인한 뒤 자연어로 대본 추출을 요청할 수도 있습니다.

- 기존 자막의 문구·공백·줄바꿈·반복·시각을 그대로 보존합니다. 요약·교정·번역하지 않습니다.
- 유튜브 자막은 `yt-dlp`와 `youtube-transcript-api`의 다른 경로로 전문을 매번 대조합니다. 캐시를 재사용해도 새로 조회합니다. 두 경로는 같은 유튜브 자막을 읽으므로 실제 발화의 정확도를 독립 검증하는 것은 아닙니다.
- 저장·원본 대조 후 임시 영상/음성·자막·전사/OCR 결과·프레임·로그·체크포인트를 삭제하고 실제 잔여를 확인합니다. **사용자가 제공한 로컬 원본은 삭제하지 않습니다.** MD·모델·설정·내용 없는 최소 영수증은 유지합니다.
- 차단·요청 제한·대조 불일치·정리 실패는 `partial`로 반환합니다. 검증 실패를 완료로 숨기거나 계정·프록시로 우회하지 않습니다. 실패 자료는 재개를 위해 남을 수 있습니다.

최종 완료는 `verification.ok`, `completion.ok`, `temporary_data_audit.ok`가 모두 `true`이고, 대상에 맞는 자막 대조와 정리가 끝난 경우입니다. 로컬/STT/OCR는 유튜브 사본 대조가 적용되지 않아 `not_applicable`로 구분합니다. 자동 자막·STT·OCR의 인식 정확도는 저장 검증과 별도입니다.

## 자원과 용량

Whisper medium CPU 2스레드·GPU 미사용, FFmpeg 1스레드, 무거운 작업 하나, 낮은 우선순위, 미디어/모델 다운로드 2MiB/s로 부하를 줄입니다. 모델 다운로드만 약 **1.56GB**이며 Python 환경·시스템 도구·Homebrew 의존성은 별도 공간을 사용합니다. ZIP에는 모델이나 개인 문서·계정·쿠키·처리 기록을 넣지 않습니다.

## 개발·검증

이 저장소만으로 빌드·회귀 검증을 재현할 수 있습니다. 별도 개인 프로젝트나 노트 서비스가 필요 없습니다.

```sh
python3.14 -m venv .venv
.venv/bin/python -m pip install -r youtube-transcript/scripts/runtime/requirements.lock
.venv/bin/python -B build.py
.venv/bin/python -B test.py
.venv/bin/python -B audit.py --tracked
.venv/bin/python -B audit.py --archive youtube-transcript-2.3.1-portable.zip
```

`build.py`는 허용한 파일만 고정 메타데이터로 묶고 SHA256·파일 manifest를 생성합니다. `audit.py`는 개인 홈/임시 경로·인증정보·연락처 패턴과 ZIP 파일 목록/해시/메타데이터를 점검합니다. 공개 보고서에는 사용자 경로·원문·쿠키·키를 기록하지 않습니다.

실제 네트워크 검증은 `.venv/bin/python -B validate.py`, macOS에서 모델 최초 다운로드와 실제 로컬/STT/OCR까지 검증하려면 `.venv/bin/python -B validate.py --full`을 실행합니다. 후자는 빈 사용자 홈에 패키지와 모델을 설치하므로 다운로드 속도 제한상 모델에 약 12분 이상 걸립니다. FFmpeg·Whisper.cpp·Tesseract는 사전 설치가 필요합니다. 종료 시 검증기가 생성한 시험 폴더와 자료를 제거합니다. 기존 모델을 명시적으로 재사용하려면 `--full --model-file FILE`을 지정할 수 있으며 보고서에서 최초 다운로드와 구분합니다.

GitHub 자동 테스트는 네트워크 자막 수집 없이 회귀·패키지/개인정보 검사를 실행합니다. 실제 공개 영상 검증은 [검증 보고서](evidence/verification.json)의 실행 범위를 확인하세요. Hermes 디렉터리 설치·실행기 검증과 실행 중인 모델의 자동 스킬 선택은 구분합니다.

외부 패키지·도구·모델은 각 프로젝트의 라이선스를 따릅니다. 목록과 상세 조건은 [설치 안내](youtube-transcript/references/install.md)에 있습니다.
