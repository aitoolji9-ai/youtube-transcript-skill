import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / 'youtube-transcript'


def module(name):
    spec = importlib.util.spec_from_file_location('portable_' + name, SKILL / f'scripts/{name}.py')
    loaded = importlib.util.module_from_spec(spec); spec.loader.exec_module(loaded)
    return loaded


setup = module('setup')
installer = module('install_skill')


class PortableTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()

    def tearDown(self): self.temp.cleanup()

    def test_install_can_move_to_path_with_spaces_and_korean(self):
        target = self.root / '새 사용자/에이전트 스킬/youtube-transcript'
        result = installer.install(target)
        self.assertEqual(result['status'], 'skill_copied')
        self.assertTrue((target / 'scripts/runtime/run').stat().st_mode & 0o111)
        self.assertFalse((target / 'scripts/runtime/.venv').exists())
        with patch.object(setup, 'SKILL', target): setup.integrity()

    def test_install_refuses_existing_folder_and_preserves_files(self):
        target = self.root / 'existing'; target.mkdir()
        sentinel = target / 'my-settings'; sentinel.write_text('keep')
        with self.assertRaises(ValueError): installer.install(target)
        self.assertEqual(sentinel.read_text(), 'keep')
        self.assertEqual(len(list(target.iterdir())), 1)

    def test_install_refuses_symlink(self):
        original = self.root / 'original'; original.mkdir()
        target = self.root / 'link'; target.symlink_to(original)
        with self.assertRaises(ValueError): installer.install(target)
        self.assertEqual(list(original.iterdir()), [])

    def test_tampered_source_cannot_be_installed(self):
        import shutil
        source = self.root / 'source'; shutil.copytree(SKILL, source)
        (source / 'SKILL.md').write_text('changed')
        with patch.object(installer, '__file__', str(source / 'scripts/install_skill.py')):
            with self.assertRaisesRegex(ValueError, '무결성'): installer.install(self.root / 'target')
        self.assertFalse((self.root / 'target').exists())

    def spec(self, data=b'model'):
        return {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest(), 'url': 'https://example.invalid/model'}

    def test_download_verifies_bytes_and_reuses_valid_model(self):
        target = self.root / 'models/model.bin'
        with patch.object(setup.urllib.request, 'urlopen', return_value=io.BytesIO(b'model')) as request:
            setup.download(target, self.spec())
            setup.download(target, self.spec())
        self.assertEqual(request.call_count, 1)
        self.assertEqual(target.read_bytes(), b'model')
        self.assertFalse(list(target.parent.glob('.model-*')))

    def test_checksum_failure_removes_partial_download(self):
        target = self.root / 'model.bin'
        with patch.object(setup.urllib.request, 'urlopen', return_value=io.BytesIO(b'wrong')):
            with self.assertRaisesRegex(ValueError, '체크섬'): setup.download(target, self.spec())
        self.assertFalse(target.exists())
        self.assertFalse(list(self.root.glob('.model-*')))

    def test_download_disconnect_cleans_staging(self):
        target = self.root / 'model.bin'
        with patch.object(setup.urllib.request, 'urlopen', side_effect=OSError('disconnect')):
            with self.assertRaises(OSError): setup.download(target, self.spec())
        self.assertFalse(target.exists())
        self.assertFalse(list(self.root.glob('.model-*')))

    def test_existing_wrong_model_is_not_overwritten(self):
        target = self.root / 'model.bin'; target.write_bytes(b'wrong')
        with patch.object(setup.urllib.request, 'urlopen') as request:
            with self.assertRaises(ValueError): setup.download(target, self.spec())
        request.assert_not_called(); self.assertEqual(target.read_bytes(), b'wrong')

    def test_too_large_download_cannot_publish(self):
        target = self.root / 'model.bin'
        with patch.object(setup.urllib.request, 'urlopen', return_value=io.BytesIO(b'model extra')):
            with self.assertRaisesRegex(ValueError, '크기'): setup.download(target, self.spec())
        self.assertFalse(target.exists()); self.assertFalse(list(self.root.glob('.model-*')))

    def test_low_disk_space_never_starts_download(self):
        from types import SimpleNamespace
        with patch.object(setup.shutil, 'disk_usage', return_value=SimpleNamespace(free=0)), patch.object(setup.urllib.request, 'urlopen') as request:
            with self.assertRaisesRegex(ValueError, '공간'): setup.download(self.root / 'model.bin', self.spec())
        request.assert_not_called()

    def test_distribution_contains_no_personal_machine_dependency(self):
        import sys
        sys.path.insert(0, str(ROOT))
        from audit import scan
        manifest = json.loads((SKILL / 'MANIFEST.json').read_text())
        self.assertEqual(scan({name: (SKILL / name).read_bytes() for name in manifest}), [])

    def prepare_fake_environment(self):
        runtime = self.root / 'runtime'
        python = runtime / '.venv/bin/python'
        python.parent.mkdir(parents=True); python.write_text('test fixture')
        return runtime, python

    def test_missing_media_tools_install_only_expected_homebrew_formulae(self):
        from types import SimpleNamespace
        runtime, python = self.prepare_fake_environment()
        installed = False

        def which(name):
            if name == 'brew': return '/tools/brew'
            return '/tools/' + name if installed else None

        def run(args, **kwargs):
            nonlocal installed
            if args[0] == '/tools/brew': installed = True
            return SimpleNamespace(returncode=0)

        with patch.object(setup, 'RUNTIME', runtime), patch.object(setup, 'STATE', self.root / 'state'), \
             patch.object(setup, 'integrity'), patch.object(setup.sys, 'platform', 'darwin'), \
             patch.object(setup.shutil, 'which', side_effect=which), patch.object(setup.subprocess, 'run', side_effect=run) as runner, \
             patch.object(setup, 'download') as download, patch.object(setup, 'check', return_value={'captions_ready': True, 'media_ready': True}):
            self.assertTrue(setup.setup(media=True, install_tools=True)['media_ready'])
        self.assertEqual(runner.call_args_list[0].args[0][0], str(python))
        self.assertEqual(runner.call_args_list[1].args[0], ['/tools/brew', 'install', 'ffmpeg', 'tesseract', 'whisper.cpp'])
        self.assertEqual(download.call_count, 3)

    def test_missing_homebrew_gives_recovery_error_before_model_download(self):
        runtime, _ = self.prepare_fake_environment()
        with patch.object(setup, 'RUNTIME', runtime), patch.object(setup, 'STATE', self.root / 'state'), \
             patch.object(setup, 'integrity'), patch.object(setup.sys, 'platform', 'darwin'), \
             patch.object(setup.shutil, 'which', return_value=None), patch.object(setup.subprocess, 'run'), \
             patch.object(setup, 'download') as download:
            with self.assertRaisesRegex(ValueError, 'Homebrew'): setup.setup(media=True, install_tools=True)
        download.assert_not_called()

    def test_missing_tools_without_install_flag_never_changes_system_packages(self):
        runtime, python = self.prepare_fake_environment()
        with patch.object(setup, 'RUNTIME', runtime), patch.object(setup, 'STATE', self.root / 'state'), \
             patch.object(setup, 'integrity'), patch.object(setup.shutil, 'which', return_value=None), \
             patch.object(setup.subprocess, 'run') as runner, patch.object(setup, 'download') as download:
            with self.assertRaisesRegex(ValueError, '미디어 도구'): setup.setup(media=True)
        self.assertEqual(runner.call_count, 1)
        self.assertEqual(runner.call_args.args[0][0], str(python))
        download.assert_not_called()


if __name__ == '__main__': unittest.main()
