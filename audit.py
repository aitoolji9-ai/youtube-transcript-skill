#!/usr/bin/env python3
"""Scan public source or archive bytes without echoing possible secret values."""
import argparse
import getpass
import hashlib
import json
from pathlib import Path
import re
import subprocess
import zipfile

PATTERNS = {
    'personal_home_path': rb'/(?:Users|home)/[A-Za-z0-9_. -]+',
    'mac_temporary_path': rb'/private/var/' + rb'folders/[^\s"\']+',
    'private_integration': rb'(?i)\b(?:hwiki' + rb'-rag|hermes' + rb'-custom|kim_[a-z]{3,10})\b',
    'provider_credential': rb'(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{30,}|sk-(?:proj-)?[A-Za-z0-9_-]{30,}|AIza[A-Za-z0-9_-]{30,}|AKIA[A-Z0-9]{16})',
    'private_key': rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
    'email': rb'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b',
    'phone': rb'(?<![A-Za-z0-9])(?:\+82[- ]?)?01[016789][- ]\d{3,4}[- ]\d{4}(?![A-Za-z0-9])',
}


def scan(files):
    patterns = dict(PATTERNS)
    user = getpass.getuser()
    if len(user) >= 4 and user not in {'root', 'runner', 'ubuntu', 'user', 'admin', 'testuser'}:
        patterns['local_account_name'] = rb'(?i)(?<![A-Za-z0-9_])' + re.escape(user.encode()) + rb'(?![A-Za-z0-9_])'
    findings = []
    for name, raw in files.items():
        for kind, pattern in patterns.items():
            for match in re.finditer(pattern, raw):
                findings.append({'file': name, 'kind': kind, 'line': raw[:match.start()].count(b'\n') + 1})
    return findings


def inspect_archive(path):
    with zipfile.ZipFile(path) as zipped:
        if zipped.testzip() is not None: raise ValueError('Corrupt archive')
        files = {}
        for info in zipped.infolist():
            p = Path(info.filename)
            if p.is_absolute() or '..' in p.parts or not info.filename.startswith('youtube-transcript/'):
                raise ValueError('Invalid archive member')
            if (info.external_attr >> 16) & 0o170000 == 0o120000: raise ValueError('Linked archive member')
            if any(part in {'.env', '.venv', '__pycache__', 'captures', 'downloads', 'jobs'} for part in p.parts):
                raise ValueError('Machine/runtime data in archive')
            if info.filename in files: raise ValueError('Duplicate archive member')
            if info.extra or info.comment: raise ValueError('Unexpected archive metadata')
            files[info.filename] = zipped.read(info)
        if zipped.comment: raise ValueError('Unexpected archive comment')
        manifest = json.loads(files['youtube-transcript/MANIFEST.json'])
        if set(files) != {'youtube-transcript/' + n for n in manifest} | {'youtube-transcript/MANIFEST.json'}:
            raise ValueError('Archive members differ from manifest')
        for name, expected in manifest.items():
            if hashlib.sha256(files['youtube-transcript/' + name]).hexdigest() != expected:
                raise ValueError('Archive checksum mismatch')
        return scan(files)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--archive', type=Path)
    p.add_argument('--tracked', action='store_true', help='Audit every Git tracked source file')
    args = p.parse_args()
    if args.archive: findings = inspect_archive(args.archive)
    elif args.tracked:
        root = Path(__file__).resolve().parent
        names = subprocess.check_output(['git', 'ls-files', '-z'], cwd=root).decode().split('\0')
        findings = scan({n: (root / n).read_bytes() for n in names if n and (root / n).is_file()})
    else: p.error('--archive or --tracked is required')
    print(json.dumps({'ok': not findings, 'findings': findings}, ensure_ascii=False))
    return 1 if findings else 0


if __name__ == '__main__': raise SystemExit(main())
