#!/usr/bin/env python3
"""Reproducibly package this repository; no sibling project or personal files."""
import hashlib
import json
from pathlib import Path
import re
import zipfile

from audit import scan

ROOT = Path(__file__).resolve().parent
SKILL = ROOT / 'youtube-transcript'
SKILL_FILES = (
    'SKILL.md', 'agents/openai.yaml', 'references/install.md', 'references/collection.md',
    'scripts/setup.py', 'scripts/install_skill.py', 'scripts/collect_transcript.py',
    'scripts/runtime/requirements.lock', 'scripts/runtime/run', 'scripts/runtime/scripts/transcribe_local.py',
)
MODULES = ('__init__', '__main__', '_child_guard', 'cli', 'completion', 'formats', 'media',
           'providers', 'resources', 'retention', 'storage')


def build():
    names = sorted([*SKILL_FILES, *(f'scripts/runtime/caption_collector/{n}.py' for n in MODULES)])
    contents = {}
    for name in names:
        path = SKILL / name
        if path.is_symlink() or not path.is_file(): raise ValueError('Missing or linked package file: ' + name)
        contents[name] = path.read_bytes()
    findings = scan(contents)
    if findings: raise ValueError('Privacy audit failed: ' + json.dumps(findings))
    manifest = {n: hashlib.sha256(raw).hexdigest() for n, raw in contents.items()}
    raw = (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode()
    (SKILL / 'MANIFEST.json').write_bytes(raw)
    contents['MANIFEST.json'] = raw
    version = re.search(r'^\s+version: "([a-z0-9.\-]+)"$', contents['SKILL.md'].decode(), re.M).group(1)
    archive = ROOT / f'youtube-transcript-{version}.zip'
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as zipped:
        for name, data in sorted(contents.items()):
            info = zipfile.ZipInfo('youtube-transcript/' + name, date_time=(2026, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (0o100755 if name == 'scripts/runtime/run' else 0o100644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            zipped.writestr(info, data)
    sha = hashlib.sha256(archive.read_bytes()).hexdigest()
    (ROOT / (archive.name + '.sha256')).write_text(sha + '  ' + archive.name + '\n')
    print(json.dumps({'archive': archive.name, 'bytes': archive.stat().st_size, 'sha256': sha,
                      'files': len(contents)}, ensure_ascii=False))
    return archive


if __name__ == '__main__': build()
