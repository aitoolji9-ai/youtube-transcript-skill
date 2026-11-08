# 설치와 점검

각 사용자가 자신의 컴퓨터에 설치한다. 제공자의 계정·쿠키·키·모델·문서·처리 기록은 배포본에 포함되지 않는다. 구글 YouTube API 키나 유료 전사 서비스 가입은 필요 없다. 처음부터 모델이 모두 들어 있는 패키지가 아니며 설치 시 필요한 공식 파일을 내려받는다.

## 스킬 등록

ZIP을 풀고 터미널에서 압축 해제한 `youtube-transcript` 폴더로 이동한다. Python 3.11~3.14를 준비한다. 아래 `python3.14`는 설치한 호환 Python 명령으로 바꿀 수 있다.

```sh
python3.14 scripts/install_skill.py codex
# Hermes에 설치하려면 대신:
python3.14 scripts/install_skill.py hermes
```

Codex는 `~/.codex/skills/youtube-transcript`, Hermes는 `~/.hermes/skills/youtube-transcript`에 복사한다. 기존 폴더가 있으면 덮어쓰지 않고 중단한다. 다른 도구는 `python3.14 scripts/install_skill.py --destination "/스킬폴더/youtube-transcript"`로 지정한다. 도구의 스킬 새로고침/다음 세션에서 목록에 보이는지 확인한다. 이 설치기는 에이전트의 실행 중인 대화가 파일을 읽었다고 판단하지 않는다.

**이후 명령은 복사된 스킬 폴더에서 실행한다.** 예를 들어 Codex는:

```sh
cd ~/.codex/skills/youtube-transcript
```

## 자막만 우선 설치

```sh
python3.14 scripts/setup.py
python3.14 scripts/setup.py --check
```

`scripts/runtime/.venv`에 잠금 버전의 패키지만 설치한다. 글로벌 Python·다른 에이전트 환경은 수정하지 않는다. `captions_ready: true`면 자막 수집·다른 경로 대조·MD 저장·정리가 준비됐다. `media_ready: false`면 자막 없는 영상의 STT와 로컬 미디어/OCR용 구성은 아직 설치되지 않은 것이다.

## 전체 기능 설치 — macOS

Python과 [Homebrew](https://brew.sh/)가 설치된 macOS:

```sh
python3.14 scripts/setup.py --media --install-tools
python3.14 scripts/setup.py --check --media
```

없는 도구만 Homebrew의 `ffmpeg`, `whisper.cpp`, `tesseract`로 설치하고, 공식 Whisper medium 및 한국어·영어 OCR 모델을 고정 리비전에서 다운로드해 SHA256을 확인한다. 모델만 약 **1.56GB(십진 단위)**이며 Python 환경·FFmpeg·Whisper.cpp·Tesseract 및 Homebrew 의존성은 별도 공간을 사용한다. 전체 설치가 2GB 미만이라고 약속하지 않는다. 설치·모델 다운로드는 최초 한 번 필요하고, 이후 재사용한다. 모델 다운로드도 최대 2MiB/s로 제한한다.

이미 도구가 설치돼 있으면 `--install-tools`를 생략한다. 본인 소유의 동일한 검증된 medium 모델이 있다면 `--media --model-file "/절대/ggml-medium.bin"`으로 복사 없이 사용할 수 있다. 해당 파일은 이후에도 유지해야 한다. 스킬은 이를 삭제하지 않는다.

`--check --media`는 다운로드·설치 없이 실제 패키지 버전, 도구 경로, 모델 크기/해시와 설정을 확인한다. `media_ready: true`가 전체 구성 점검 통과다. 설치 명령 성공만으로 영상 추출 성공을 판단하지 않는다.

## Linux와 Windows

macOS에서 실제 설치·추출을 검증한 배포본이다. Linux/WSL은 POSIX 잠금과 낮은 우선순위 경로를 포함하지만 이 배포 시점의 실제 종단 검증 범위에는 포함하지 않는다. Windows 기본 Python은 지원하지 않는다.

Linux/WSL 사용자는 배포판의 Python 3.11~3.14·venv·FFmpeg/FFprobe·Tesseract와 공식 [Whisper.cpp](https://github.com/ggml-org/whisper.cpp)의 `whisper-cli`를 설치해 PATH에 둔 다음 `python3 scripts/setup.py --media`를 실행한다. 이 설치기는 sudo·운영체제 패키지 관리자를 자동 실행하지 않는다.

## 첫 추출

```sh
python3 scripts/collect_transcript.py "https://youtu.be/VIDEO_ID"
python3 scripts/collect_transcript.py "/절대/영상.mp4" --language ko
```

출력은 자신의 `~/Documents/대본MD`이다. JSON의 `completion.ok`와 `temporary_data_audit.ok`, 해당 소스에 맞는 `caption_audit.status`를 확인하고 MD를 열어 본다. API 차단·로그인·언어 선택은 [수집과 복구](collection.md)를 따른다.

## 업데이트와 제거

기존 스킬을 새 ZIP으로 합쳐 덮어쓰지 않는다. 작업이 끝난 뒤 기존 스킬 폴더를 다른 이름으로 보관하고 새 폴더를 설치한 후 설정 점검·짧은 실제 추출을 확인한다. 상태 경로는 버전 간 재사용한다. 추출 문서와 사용자 원본은 스킬 폴더 밖에 있으므로 스킬을 제거해도 유지된다. 재사용 모델·최소 영수증까지 없애려면 추출 작업이 없는 것을 확인하고 본인의 `~/.local/share/youtube-transcript`만 별도로 삭제한다.

## 외부 구성요소

배포 ZIP에는 자체 실행 코드·고정 Python 의존성 목록만 들어 있다. 설치 시 내려받는 외부 패키지/도구/모델은 각 프로젝트의 라이선스를 따른다: [yt-dlp](https://github.com/yt-dlp/yt-dlp), [youtube-transcript-api](https://github.com/jdepoix/youtube-transcript-api), [FFmpeg](https://ffmpeg.org/), [Whisper.cpp](https://github.com/ggml-org/whisper.cpp), [Whisper 모델](https://huggingface.co/ggerganov/whisper.cpp), [Tesseract](https://github.com/tesseract-ocr/tesseract), [OCR 모델](https://github.com/tesseract-ocr/tessdata_best). 유튜브 서비스 접근 제한과 각 사용자의 시청·사용 권한을 따른다.
