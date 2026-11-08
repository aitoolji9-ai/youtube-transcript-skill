import io
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from audit import inspect_archive, scan


class AuditTests(unittest.TestCase):
    def test_personal_path_is_detected_without_echoing_value(self):
        private = b'/' + b'Users' + b'/private-person/work'
        result = scan({'sample': private})
        self.assertTrue(any(r['kind'] == 'personal_home_path' for r in result))
        self.assertNotIn(private.decode(), str(result))

    def test_credentials_are_detected_without_echoing_value(self):
        secret = b'gh' + b'p_' + b'x' * 40
        result = scan({'sample': secret})
        self.assertEqual(result[0]['kind'], 'provider_credential')
        self.assertNotIn(secret.decode(), str(result))

    def test_dynamic_user_directory_is_allowed(self):
        self.assertEqual(scan({'sample': b'Path.home() / "Documents" / "transcripts"'}), [])

    def test_unlisted_archive_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'bad.zip'
            with zipfile.ZipFile(path, 'w') as z:
                z.writestr('youtube-transcript/MANIFEST.json', '{}')
                z.writestr('youtube-transcript/unlisted.txt', 'not allowed')
            with self.assertRaisesRegex(ValueError, 'manifest'): inspect_archive(path)

    def test_archive_comment_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'bad.zip'
            with zipfile.ZipFile(path, 'w') as z:
                z.writestr('youtube-transcript/MANIFEST.json', '{}')
                z.comment = b'not package data'
            with self.assertRaisesRegex(ValueError, 'comment'): inspect_archive(path)


if __name__ == '__main__': unittest.main()
