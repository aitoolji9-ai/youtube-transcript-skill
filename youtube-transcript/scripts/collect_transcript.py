#!/usr/bin/env python3
"""Portable entry point. Credentials and settings belong to this computer's user."""
import json
import os
from pathlib import Path
import sys


def main():
    runtime = Path(__file__).resolve().parent / 'runtime'
    python = runtime / '.venv/bin/python'
    if '--print-default-output' in sys.argv[1:]:
        print(Path.home() / 'Documents/대본MD')
        return
    if not python.is_file():
        print(json.dumps({'status': 'not_installed', 'message': '먼저 Python 3.11~3.14로 scripts/setup.py를 실행하세요.'}, ensure_ascii=False))
        raise SystemExit(2)
    os.chdir(runtime)
    os.execv(str(python), [str(python), '-B', '-m', 'caption_collector', *sys.argv[1:]])


if __name__ == '__main__': main()
