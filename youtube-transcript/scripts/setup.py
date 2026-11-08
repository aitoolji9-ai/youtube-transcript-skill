#!/usr/bin/env python3
"""Isolated dependencies and checksum-verified optional local models."""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import venv

SKILL = Path(__file__).resolve().parents[1]
RUNTIME = SKILL / 'scripts/runtime'
STATE = Path.home() / '.local/share/youtube-transcript'
MODEL = {'bytes': 1533763059, 'sha256': '6c14d5adee5f86394037b4e4e8b59f1673b6cee10e3cf0b11bbdbee79c156208',
         'url': 'https://huggingface.co/ggerganov/whisper.cpp/resolve/5359861c739e955e79d9a303bcbc70fb988958b1/ggml-medium.bin'}
OCR_REVISION = 'e12c65a915945e4c28e237a9b52bc4a8f39a0cec'
OCR = {'kor': {'bytes': 12528128, 'sha256': 'f888d4038348a0c3d25151e7f452bda0d74ca275b18cab146798bcbb94084fff'},
       'eng': {'bytes': 15400601, 'sha256': '8280aed0782fe27257a68ea10fe7ef324ca0f8d85bd2fd145d1c2b560bcb66ba'}}


def file_hash(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b''): h.update(block)
    return h.hexdigest()


def valid(path, spec):
    return (not path.is_symlink() and path.is_file() and path.stat().st_size == spec['bytes']
            and file_hash(path) == spec['sha256'])


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.setup-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp): os.unlink(temp)


def download(path, spec):
    if valid(path, spec): return
    if path.exists() or path.is_symlink():
        raise ValueError('기존 모델이 예상 해시와 다릅니다. 덮어쓰지 않습니다: ' + str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(path.parent).free < spec['bytes'] + 64 * 1024 * 1024:
        raise ValueError('모델 다운로드 공간이 부족합니다: ' + str(path.parent))
    fd, temp = tempfile.mkstemp(prefix='.model-', dir=path.parent)
    started = time.monotonic(); total = 0; last_progress = started
    try:
        with os.fdopen(fd, 'wb') as stream:
            with urllib.request.urlopen(spec['url'], timeout=30) as response:
                while block := response.read(1024 * 1024):
                    total += len(block)
                    if total > spec['bytes']: raise ValueError('예상 모델 크기를 초과했습니다.')
                    stream.write(block)
                    delay = total / (2 * 1024 * 1024) - (time.monotonic() - started)
                    if delay > 0: time.sleep(delay)
                    if time.monotonic() - last_progress >= 30:
                        print(json.dumps({'event': 'model_download', 'model': path.name,
                                          'downloaded_bytes': total, 'total_bytes': spec['bytes']}), file=sys.stderr, flush=True)
                        last_progress = time.monotonic()
            stream.flush(); os.fsync(stream.fileno())
        if not valid(Path(temp), spec): raise ValueError('모델 다운로드 체크섬이 다릅니다.')
        # A concurrent installer must not overwrite a model published first.
        try: os.link(temp, path)
        except FileExistsError:
            if not valid(path, spec): raise ValueError('동시에 생성된 모델이 일치하지 않습니다.')
    finally:
        if os.path.exists(temp): os.unlink(temp)


def integrity():
    manifest = json.loads((SKILL / 'MANIFEST.json').read_text())
    for name, expected in manifest.items():
        relative = Path(name)
        if relative.is_absolute() or '..' in relative.parts: raise ValueError('잘못된 manifest 경로')
        path = SKILL / name
        if path.is_symlink() or not path.is_file() or file_hash(path) != expected:
            raise ValueError('배포 파일 무결성 확인 실패: ' + name)


def check():
    python = RUNTIME / '.venv/bin/python'
    dependencies = {}
    if python.is_file():
        code = 'import importlib.metadata,json; print(json.dumps({n:importlib.metadata.version(n) for n in ("yt-dlp","youtube-transcript-api")}))'
        proc = subprocess.run([str(python), '-B', '-c', code], capture_output=True, text=True)
        if proc.returncode == 0: dependencies = json.loads(proc.stdout)
    expected = dict(line.split('==') for line in (RUNTIME / 'requirements.lock').read_text().splitlines() if line)
    captions = all(dependencies.get(n) == expected[n] for n in ('yt-dlp', 'youtube-transcript-api'))
    tools = {name: shutil.which(name) for name in ('ffmpeg', 'ffprobe', 'whisper-cli', 'tesseract')}
    config = STATE / 'stt/config.json'
    medium = False
    if config.is_file():
        try:
            data = json.loads(config.read_text())
            medium = (valid(Path(data['model_path']), MODEL) and data['model'] == 'medium'
                      and data['threads'] == 2 and data['gpu'] is False and data['sha256'] == MODEL['sha256'])
        except (ValueError, KeyError, OSError): pass
    ocr = all(valid(STATE / f'ocr-models/{name}.traineddata', spec) for name, spec in OCR.items())
    manifest = STATE / 'ocr-models/manifest.json'
    try: ocr = ocr and json.loads(manifest.read_text()).get('sha256') == OCR['kor']['sha256']
    except (ValueError, OSError): ocr = False
    return {'captions_ready': captions, 'dependencies': dependencies, 'media_ready': captions and all(tools.values()) and medium and ocr,
            'tools': tools, 'medium_verified': medium, 'ocr_verified': ocr,
            'output_dir': str(Path.home() / 'Documents/대본MD'), 'state_dir': str(STATE),
            'api_key_required': False}


def setup(media=False, install_tools=False, model_file=None):
    integrity()
    if os.name != 'posix': raise ValueError('macOS 또는 Linux/WSL에서 실행하세요. Windows 기본 Python은 지원하지 않습니다.')
    if not (3, 11) <= sys.version_info[:2] < (3, 15):
        raise ValueError('Python 3.11~3.14가 필요합니다. 해당 Python으로 다시 실행하세요.')
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Two setup invocations must not modify the same environment/config together.
    import fcntl
    with (STATE / 'setup.lock').open('a') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise ValueError('다른 설치가 실행 중입니다.')
        python = RUNTIME / '.venv/bin/python'
        if not python.is_file(): venv.EnvBuilder(with_pip=True).create(RUNTIME / '.venv')
        subprocess.run([str(python), '-m', 'pip', 'install', '--disable-pip-version-check', '--no-cache-dir',
                        '-r', str(RUNTIME / 'requirements.lock')], check=True)
        if media:
            missing = [name for name in ('ffmpeg', 'ffprobe', 'whisper-cli', 'tesseract') if not shutil.which(name)]
            if missing and install_tools:
                brew = shutil.which('brew')
                if sys.platform != 'darwin' or not brew:
                    raise ValueError('자동 도구 설치는 Homebrew가 있는 macOS에서 지원합니다. references/install.md를 확인하세요.')
                formulae = sorted({'ffmpeg' if n in ('ffmpeg', 'ffprobe') else 'whisper.cpp' if n == 'whisper-cli' else 'tesseract' for n in missing})
                subprocess.run([brew, 'install', *formulae], check=True)
                missing = [name for name in ('ffmpeg', 'ffprobe', 'whisper-cli', 'tesseract') if not shutil.which(name)]
            if missing: raise ValueError('미디어 도구를 설치하세요: ' + ', '.join(missing) + '. references/install.md 참고')
            model = Path(model_file).expanduser().resolve(strict=True) if model_file else STATE / 'stt/models/ggml-medium.bin'
            if model_file:
                if not valid(model, MODEL): raise ValueError('지정한 파일이 검증된 Whisper medium 모델과 다릅니다.')
            else: download(model, MODEL)
            write_json(STATE / 'stt/config.json', {'model': 'medium', 'model_path': str(model), 'sha256': MODEL['sha256'],
                       'bytes': MODEL['bytes'], 'threads': 2, 'processors': 1, 'gpu': False,
                       'nice': 19, 'taskpolicy_background': sys.platform == 'darwin', 'max_concurrent_jobs': 1})
            for name, spec in OCR.items():
                download(STATE / f'ocr-models/{name}.traineddata',
                         dict(spec, url=f'https://raw.githubusercontent.com/tesseract-ocr/tessdata_best/{OCR_REVISION}/{name}.traineddata'))
            write_json(STATE / 'ocr-models/manifest.json', {'repository': 'tesseract-ocr/tessdata_best',
                       'revision': OCR_REVISION, **OCR['kor'], 'eng_sha256': OCR['eng']['sha256'], 'language': 'kor+eng'})
        result = check()
        if not result['captions_ready'] or (media and not result['media_ready']):
            raise ValueError('설치 후 실제 구성 확인을 통과하지 못했습니다: ' + json.dumps(result, ensure_ascii=False))
        return result


def main():
    parser = argparse.ArgumentParser(description='대본 스킬 전용 환경 설치 및 점검. API 키 불필요.')
    parser.add_argument('--media', action='store_true', help='Whisper medium 및 한국어·영어 OCR 설치(모델 약 1.56GB + 실행 도구)')
    parser.add_argument('--install-tools', action='store_true', help='필요한 시스템 도구를 Homebrew로 설치(--media 필요)')
    parser.add_argument('--model-file', type=Path, help='이미 보유한 검증된 ggml-medium.bin을 복사 없이 사용(--media 필요)')
    parser.add_argument('--check', action='store_true', help='설치·다운로드 없이 실제 구성 확인')
    args = parser.parse_args()
    if (args.install_tools or args.model_file) and not args.media: parser.error('--media와 함께 사용하세요.')
    try:
        if args.check:
            integrity(); result = check()
        else: result = setup(args.media, args.install_tools, args.model_file)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result['captions_ready'] and (not args.media or result['media_ready']) else 2
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(json.dumps({'status': 'setup_failed', 'message': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__': raise SystemExit(main())
