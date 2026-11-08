#!/usr/bin/env python3
"""Test the release ZIP in an empty HOME and publish only path-free evidence."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

from audit import inspect_archive

ROOT = Path(__file__).resolve().parent
DEFAULT_ARCHIVE = ROOT / 'youtube-transcript-2.3.1-portable.zip'
DEFAULT_VIDEO = 'https://youtu.be/jKupp4CYJAc'


def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--archive', type=Path, default=DEFAULT_ARCHIVE)
    p.add_argument('--full', action='store_true', help='Also install/download medium and OCR; test real local media on macOS')
    p.add_argument('--model-file', type=Path, help='Optional explicit reuse; omit for a genuine first model download')
    p.add_argument('--url', default=DEFAULT_VIDEO)
    args = p.parse_args()
    if args.model_file and not args.full: p.error('--model-file needs --full')
    if args.full and sys.platform != 'darwin': p.error('Full live synthetic-media validation currently needs macOS say/fonts')
    if inspect_archive(args.archive): raise ValueError('Archive privacy audit failed')
    (ROOT / 'evidence').mkdir(exist_ok=True)
    evidence = {'archive_sha256': sha(args.archive), 'started_at': datetime.now(timezone.utc).isoformat(),
                'scope': 'fresh_home_install_and_real_entrypoints', 'platform': sys.platform,
                'full_media_requested': args.full, 'personal_paths_in_report': False}
    work = Path(tempfile.mkdtemp(prefix='youtube-transcript-release-')).resolve()
    (ROOT / 'evidence/active-test-root.json').write_text(json.dumps({'path': str(work)}))
    home = work / 'recipient home'; home.mkdir()
    env = dict(os.environ, HOME=str(home), PYTHONDONTWRITEBYTECODE='1')
    for name in ('PYTHONPATH', 'VIRTUAL_ENV', 'HERMES_HOME', 'HERMES_SESSION_PROFILE'):
        env.pop(name, None)

    def execute(args, *, cwd=None, json_output=False, expected=0, timeout=120):
        proc = subprocess.run([str(a) for a in args], cwd=cwd, env=env, text=True, capture_output=True, timeout=timeout)
        if proc.returncode != expected:
            raise RuntimeError(f'Subprocess failed: {Path(args[0]).name}, exit={proc.returncode}')
        return json.loads(proc.stdout) if json_output else proc

    def extract(installed, source, *options):
        started = time.monotonic()
        result = execute([sys.executable, installed / 'scripts/collect_transcript.py', source, *options], json_output=True, timeout=300)
        assert result['completion']['ok'] and result['verification']['ok']
        assert result['temporary_data_audit']['ok'] and result['retention']['status'] == 'complete'
        note = Path(result['files']['note']['path'])
        assert note.parent == home / 'Documents/대본MD'
        assert note.is_file() and sha(note) == result['files']['note']['sha256']
        assert set(result['files']) == {'note'}
        assert set(p.name for p in Path(result['receipt']).parent.iterdir()) == {'receipt.json'}
        expected = 'passed' if str(source).startswith('https://') else 'not_applicable'
        assert result['caption_audit']['status'] == expected
        state = home / '.local/share/youtube-transcript'
        payload = []
        for folder in ('media', 'requests', 'stt/jobs', 'downloads'):
            if (state / folder).exists(): payload += [p for p in (state / folder).rglob('*') if p.is_file()]
        assert not payload
        # Inspect every capture directory, not only the finalizer's counters.
        unexpected = [p for p in (state / 'captures').rglob('*') if p.is_file() and p.name != 'receipt.json']
        assert not unexpected
        assert all(p.suffix == '.md' for p in note.parent.iterdir())
        offline = execute([sys.executable, installed / 'scripts/collect_transcript.py', '--verify', result['receipt']], json_output=True)
        assert offline['verification']['ok']
        return {'status': result['status'], 'provider': result['provider'], 'segment_count': result['segment_count'],
                'note_sha256': sha(note), 'caption_audit': result['caption_audit'],
                'temporary_data_audit': result['temporary_data_audit'], 'completion': result['completion'],
                'offline_readback': True, 'independent_payload_count': len(payload) + len(unexpected),
                'elapsed_seconds': round(time.monotonic() - started, 2)}, note.read_text()

    try:
        with zipfile.ZipFile(args.archive) as zipped:
            evidence['archive_file_count'] = len(zipped.namelist())
            zipped.extractall(work / 'unpacked')
        unpacked = work / 'unpacked/youtube-transcript'
        execute([sys.executable, unpacked / 'scripts/install_skill.py', 'codex'], json_output=True)
        installed = home / '.codex/skills/youtube-transcript'
        evidence['codex_install'] = 'passed'
        execute([sys.executable, unpacked / 'scripts/install_skill.py', 'codex'], expected=2)
        evidence['existing_install_preserved'] = True
        result = execute([sys.executable, installed / 'scripts/collect_transcript.py', args.url], expected=2, json_output=True)
        assert result['status'] == 'not_installed'
        evidence['not_installed_result'] = result['status']
        command = [sys.executable, installed / 'scripts/setup.py']
        if args.full: command += ['--media']
        if args.model_file: command += ['--model-file', str(args.model_file.expanduser().resolve())]
        # Stream content-free download progress; dependency-install output is
        # discarded rather than written into shareable validation evidence.
        subprocess.run(command, env=env, stdout=subprocess.DEVNULL, check=True, timeout=1800)
        check = execute([sys.executable, installed / 'scripts/setup.py', '--check', *(['--media'] if args.full else [])], json_output=True)
        assert check['captions_ready'] and (not args.full or check['media_ready'])
        evidence['setup'] = {k: check[k] for k in ('captions_ready', 'media_ready', 'dependencies', 'medium_verified', 'ocr_verified', 'api_key_required')}
        evidence['system_tools'] = {k: bool(v) for k, v in check['tools'].items()}
        evidence['medium_install_path'] = 'explicit_reuse_sha256_verified' if args.model_file else 'fresh_official_download_sha256_verified' if args.full else 'not_requested'
        print('Fresh recipient setup passed', flush=True)
        runtime = installed / 'scripts/runtime'
        proc = execute([runtime / '.venv/bin/python', '-B', ROOT / 'test.py'], cwd=runtime)
        import re
        count = re.search(r'Ran (\d+) tests', proc.stderr)
        evidence['regression_tests'] = {'run': int(count.group(1)), 'passed': int(count.group(1))}
        print('Self-contained regression suite passed', flush=True)
        evidence['youtube'], _ = extract(installed, args.url, '--mode', 'captions')
        evidence['youtube_cached_recheck'], _ = extract(installed, args.url, '--mode', 'captions')
        assert evidence['youtube_cached_recheck']['caption_audit']['checked_at'] != evidence['youtube']['caption_audit']['checked_at']
        print('YouTube extraction and fresh cache audit passed', flush=True)
        if args.full:
            inputs = work / 'test-inputs'; inputs.mkdir()
            srt = inputs / 'original.srt'
            srt.write_text('1\n00:00:00,125 --> 00:00:00,900\n원문  그대로\n반복\n\n2\n00:00:01,000 --> 00:00:01,900\n마지막 구간\n')
            embedded = inputs / 'embedded.mkv'
            execute(['ffmpeg', '-v', 'error', '-filter_threads', '1', '-threads', '1', '-f', 'lavfi', '-i', 'color=c=black:s=640x360:d=2:r=2',
                     '-i', srt, '-map', '0:v', '-map', '1:s', '-c:v', 'mpeg4', '-threads', '1', '-c:s', 'srt', '-metadata:s:s:0', 'language=kor', embedded])
            original = sha(embedded)
            evidence['embedded'], content = extract(installed, embedded, '--language', 'ko')
            assert '원문  그대로\n반복' in content and '마지막 구간' in content and sha(embedded) == original
            print('Local embedded caption preservation passed', flush=True)
            screen = inputs / 'screen.mp4'
            font = '/System/Library/Fonts/Supplemental/Arial.ttf'
            execute(['ffmpeg', '-v', 'error', '-filter_threads', '1', '-threads', '1', '-f', 'lavfi', '-i', 'color=c=white:s=960x540:d=2:r=2',
                     '-vf', f'drawtext=fontfile={font}:text=PORTABLE TEST:fontcolor=black:fontsize=58:x=(w-tw)/2:y=(h-th)/2',
                     '-c:v', 'mpeg4', '-threads', '1', screen])
            original = sha(screen)
            evidence['ocr'], content = extract(installed, screen, '--mode', 'ocr', '--language', 'en')
            assert 'PORTABLE TEST' in content and sha(screen) == original
            print('Screen OCR and original preservation passed', flush=True)
            audio = inputs / 'audio.aiff'
            execute(['/usr/bin/say', '-v', 'Samantha', '-r', '150', '-o', audio, 'This is a local transcript test. Keep the original audio file.'])
            original = sha(audio)
            evidence['stt'], content = extract(installed, audio, '--mode', 'stt', '--language', 'en')
            assert evidence['stt']['provider'] == 'whisper_medium' and evidence['stt']['segment_count'] > 0 and sha(audio) == original
            evidence['local_originals_preserved_until_test_cleanup'] = True
            print('Real Whisper medium transcription and cleanup passed', flush=True)
        execute([sys.executable, unpacked / 'scripts/install_skill.py', 'hermes'], json_output=True)
        evidence['hermes_install'] = 'passed'
        hermes = home / '.hermes/skills/youtube-transcript'
        execute([sys.executable, hermes / 'scripts/setup.py'], timeout=90)
        evidence['hermes_registry_entrypoint'], _ = extract(hermes, args.url, '--mode', 'captions')
        evidence['hermes_model_autoselection'] = 'not_tested'
        evidence['ok'] = True
        print('Hermes skill-directory entrypoint passed', flush=True)
    except Exception as exc:
        evidence['ok'] = False
        evidence['error_type'] = type(exc).__name__
        raise
    finally:
        shutil.rmtree(work)
        evidence['isolated_test_workspace_removed'] = not work.exists()
        evidence['finished_at'] = datetime.now(timezone.utc).isoformat()
        (ROOT / 'evidence/active-test-root.json').unlink(missing_ok=True)
        (ROOT / 'evidence/verification.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps({'ok': evidence.get('ok'), 'workspace_removed': evidence['isolated_test_workspace_removed']}))


if __name__ == '__main__': main()
