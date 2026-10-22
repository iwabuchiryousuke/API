import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from diagnostic_bundle import create_bundle


class DiagnosticBundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name) / 'application'
        self.base.mkdir()
        self.destination = Path(self.temp.name) / 'diagnostics.zip'

    def tearDown(self):
        self.temp.cleanup()

    def write(self, relative, content):
        path = self.base / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode('utf-8'))
        return path

    def contents(self, result):
        with ZipFile(result['path']) as bundle:
            return {name: bundle.read(name) for name in bundle.namelist()}

    def test_only_allowlisted_sources_and_no_private_pdf_or_database(self):
        self.write('data/diagnostics/auto_01.txt', 'PDF収集を開始しました。')
        self.write('data/diagnostics/navigation_A_01.json', '{"stage":"navigation"}')
        self.write('data/diagnostics/assets_A_01.json', '{"name":"口座本人", "value":1234}')
        self.write('data/jobs/job1/state.json', '{"phase":"COMPLETED"}')
        self.write('data/電子ポストPDF/A/取得結果_01.json', '{"expected":3}')
        self.write('data/diagnostics/assets_A_01.png', b'private-image-content')
        for name in ('data/private/login_A.dat', 'data/private/gmail_A.secure',
                     'data/ledger.sqlite3', 'data/originals/A/source.pdf',
                     'data/電子ポストPDF/A/source.pdf', 'personal_setup.enc',
                     'secret.key', 'data/diagnostics/anything.json',
                     'data/diagnostics/auto_private.secure'):
            self.write(name, b'SHOULD_NOT_APPEAR')
        result = create_bundle(self.base, self.destination)
        entries = self.contents(result)
        self.assertEqual(len(result['files']), 5)
        self.assertTrue(result['contains_account_information'])
        self.assertIn(b'Account names', entries['manifest.json'])
        self.assertIn('口座本人', entries['diagnostics/assets_01.json'].decode('utf-8'))
        self.assertNotIn(b'SHOULD_NOT_APPEAR', b''.join(entries.values()))
        self.assertFalse(any(name.endswith(('.pdf', '.sqlite3', '.png', '.dat', '.secure')) for name in entries))

    def test_redacts_urls_tokens_passwords_and_otp(self):
        secrets = ('fixture-user', 'fixture-password', 'fixture-session', 'fixture-access',
                   'fixture-refresh', 'fixture-id', 'fixture-api-key', '123456',
                   'fixture-bearer', 'fixture-cookie')
        text = ('failure https://fixture-user:fixture-password@example.com/private/fixture-session'
                '?access_token=fixture-access#fixture-refresh\n'
                'Authorization: Bearer fixture-bearer\npassword = fixture-password\n'
                '認証コード\n123456\nCookie: fixture-cookie\n')
        self.write('data/diagnostics/auto_01.txt', text)
        self.write('data/diagnostics/assets_A.json', json.dumps({
            'access_token': 'fixture-access', 'refresh_token': 'fixture-refresh',
            'id_token': 'fixture-id', 'api_key': 'fixture-api-key',
            'nested': {'OTP': 123456, 'password': 'fixture-password'},
            'url': 'https://example.com/private/fixture-session?code=123456',
            'href': '/private/fixture-session?code=123456'}))
        contents = b''.join(self.contents(create_bundle(self.base, self.destination)).values()).decode('utf-8')
        for secret in secrets:
            self.assertNotIn(secret, contents)
        self.assertIn('https://example.com', contents)
        self.assertIn('[REDACTED]', contents)

    def test_secret_dumps_and_ambiguous_plaintext_are_omitted(self):
        self.write('data/diagnostics/auto_01.txt', '-----BEGIN PRIVATE KEY-----\nfixture-pem')
        self.write('data/diagnostics/auto_02.txt', 'password is fixture-unknown')
        self.write('data/diagnostics/auto_03.txt', 'PATH=/private/fixture-path\nHOME=/private/fixture-home')
        self.write('data/diagnostics/assets_A.json', '{"credentials":{"token":"fixture-token"}}')
        self.write('data/diagnostics/navigation_A.json', '{"environment":{"HOME":"fixture-home"}}')
        result = create_bundle(self.base, self.destination)
        self.assertEqual(result['files'], [])
        contents = b''.join(self.contents(result).values())
        self.assertNotIn(b'fixture-', contents)
        self.assertEqual(len(result['skipped']), 5)

    def test_symlink_files_and_parent_directories_are_not_followed(self):
        outside = Path(self.temp.name) / 'outside-secret.txt'
        outside.write_text('outside-secret')
        diagnostics = self.base / 'data/diagnostics'
        diagnostics.mkdir(parents=True)
        try:
            (diagnostics / 'auto_01.txt').symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest('Symlinks unavailable')
        result = create_bundle(self.base, self.destination)
        self.assertEqual(result['files'], [])
        (diagnostics / 'auto_01.txt').unlink()
        diagnostics.rmdir()
        outside_dir = Path(self.temp.name) / 'outside'
        outside_dir.mkdir()
        (outside_dir / 'auto_02.txt').write_text('outside-secret')
        diagnostics.symlink_to(outside_dir, target_is_directory=True)
        result = create_bundle(self.base, self.destination)
        self.assertEqual(result['files'], [])
        self.assertNotIn(b'outside-secret', b''.join(self.contents(result).values()))

    def test_final_compressed_size_never_exceeds_limit(self):
        for index in range(4):
            self.write(f'data/diagnostics/auto_{index}.txt', os.urandom(12000).hex())
        result = create_bundle(self.base, self.destination, max_bytes=2048)
        self.assertLessEqual(self.destination.stat().st_size, 2048)
        self.assertTrue(result['skipped'])
        self.assertEqual(result['files'], [])

    def test_input_limit_invalid_json_and_binary_are_skipped(self):
        self.write('data/diagnostics/auto_big.txt', 'x' * 100)
        self.write('data/diagnostics/navigation_A.json', '{invalid')
        self.write('data/diagnostics/auto_binary.txt', b'\x00unknown')
        with patch('diagnostic_bundle.MAX_SOURCE_BYTES', 50):
            result = create_bundle(self.base, self.destination)
        self.assertEqual(result['files'], [])
        self.assertEqual(len(result['skipped']), 3)

    def test_only_latest_logs_and_latest_acquisition_audit(self):
        for index in range(7):
            p = self.write(f'data/diagnostics/auto_{index}.txt', f'run-{index}')
            os.utime(p, (index + 10, index + 10))
        for index in range(3):
            p = self.write(f'data/電子ポストPDF/A/取得結果_{index}.json', json.dumps({'run': index}))
            os.utime(p, (index + 10, index + 10))
        contents = self.contents(create_bundle(self.base, self.destination))
        logs = [value for name, value in contents.items() if name.endswith('.txt')]
        self.assertEqual(logs, [b'run-6', b'run-5', b'run-4', b'run-3'])
        self.assertEqual(json.loads(contents['acquisition/A.json'])['run'], 2)

    def test_source_bytes_are_unchanged(self):
        source = self.write('data/diagnostics/auto_01.txt', 'password=fixture-password')
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        create_bundle(self.base, self.destination)
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)

    def test_small_limit_does_not_replace_existing_destination(self):
        self.destination.write_bytes(b'existing-file')
        with self.assertRaises(ValueError):
            create_bundle(self.base, self.destination, max_bytes=10)
        self.assertEqual(self.destination.read_bytes(), b'existing-file')

    def test_output_symlink_is_refused(self):
        outside = Path(self.temp.name) / 'unrelated.zip'
        outside.write_bytes(b'existing-file')
        try:
            self.destination.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest('Symlinks unavailable')
        with self.assertRaises(ValueError):
            create_bundle(self.base, self.destination)
        self.assertEqual(outside.read_bytes(), b'existing-file')

    def test_archive_paths_are_canonical_not_source_names(self):
        self.write('data/diagnostics/auto_password-fixture-secret.txt', 'safe-log')
        contents = self.contents(create_bundle(self.base, self.destination))
        self.assertIn('diagnostics/auto_01.txt', contents)
        self.assertFalse(any('fixture-secret' in name or '..' in Path(name).parts for name in contents))

    def test_nested_dump_and_uri_in_json_key_are_handled(self):
        self.write('data/diagnostics/assets_A.json', json.dumps({
            'https://example.com/fixture-path?token=fixture-token': 'safe',
            'code': 'fixture-otp'}))
        self.write('data/diagnostics/navigation_A.json', json.dumps({
            'text': 'HOME=/fixture-home\nPATH=/fixture-path'}))
        result = create_bundle(self.base, self.destination)
        content = b''.join(self.contents(result).values()).decode('utf-8')
        for secret in ('fixture-path', 'fixture-token', 'fixture-otp', 'fixture-home'):
            self.assertNotIn(secret, content)
        self.assertEqual(len(result['skipped']), 1)

    def test_total_input_limit_is_enforced_before_next_read(self):
        self.write('data/diagnostics/auto_01.txt', 'short log one')
        self.write('data/diagnostics/auto_02.txt', 'short log two')
        with patch('diagnostic_bundle.MAX_TOTAL_INPUT_BYTES', 15):
            result = create_bundle(self.base, self.destination)
        self.assertEqual(len(result['files']), 1)
        self.assertEqual(len(result['skipped']), 1)

    def test_caller_cannot_raise_the_absolute_bundle_cap(self):
        self.write('data/diagnostics/auto_01.txt', os.urandom(4000).hex())
        with patch('diagnostic_bundle.MAX_BUNDLE_BYTES', 2048):
            result = create_bundle(self.base, self.destination, max_bytes=1024 * 1024)
        self.assertLessEqual(self.destination.stat().st_size, 2048)
        self.assertEqual(result['files'], [])


if __name__ == '__main__':
    unittest.main()
