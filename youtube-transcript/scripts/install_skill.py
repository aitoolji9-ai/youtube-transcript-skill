#!/usr/bin/env python3
"""Register only this allowlisted skill, refusing an existing installation."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys


def install(target):
    source = Path(__file__).resolve().parents[1]
    manifest = json.loads((source / 'MANIFEST.json').read_text())
    for name, sha in manifest.items():
        relative = Path(name)
        if relative.is_absolute() or '..' in relative.parts: raise ValueError('잘못된 manifest 경로')
        path = source / relative
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != sha:
            raise ValueError('배포 파일 무결성 확인 실패: ' + name)
    target = target.expanduser().absolute()
    if target == source or target.is_relative_to(source): raise ValueError('배포 원본 폴더 안에는 설치하지 않습니다.')
    if target.exists() or target.is_symlink(): raise ValueError('기존 스킬을 보존하기 위해 중단합니다: ' + str(target))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.mkdir()  # Exclusive creation; never merge into another installation.
    try:
        for name in [*manifest, 'MANIFEST.json']:
            path = target / name; path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / name, path)
            if name == 'scripts/runtime/run': path.chmod(0o755)
            if name in manifest and hashlib.sha256(path.read_bytes()).hexdigest() != manifest[name]:
                raise ValueError('복사 후 무결성 확인 실패: ' + name)
    except BaseException:
        shutil.rmtree(target)
        raise
    return {'status': 'skill_copied', 'path': str(target),
            'next_step': '이 폴더에서 Python 3.11~3.14로 scripts/setup.py --media --install-tools 실행(macOS/Homebrew). references/install.md 참고'}


def main():
    p = argparse.ArgumentParser(description='독립 대본 스킬을 복사합니다. 기존 설치를 덮어쓰지 않습니다.')
    p.add_argument('agent', nargs='?', choices=['codex', 'hermes'])
    p.add_argument('--destination', type=Path)
    args = p.parse_args()
    if bool(args.agent) == bool(args.destination): p.error('codex/hermes 또는 --destination 중 하나를 지정하세요.')
    target = args.destination or (Path.home() / ('.codex' if args.agent == 'codex' else '.hermes') / 'skills/youtube-transcript')
    try: print(json.dumps(install(target), ensure_ascii=False))
    except (OSError, ValueError) as exc:
        print(json.dumps({'status': 'install_failed', 'message': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__': raise SystemExit(main())
