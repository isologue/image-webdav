import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from passlib.hash import apr_md5_crypt
import auth


class AuthTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for name, value in [('CONFIG', self.root), ('SETTINGS', self.root / 'settings.json')]:
            patcher = patch.object(auth, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_fresh_defaults_and_persistence(self):
        secret = auth.prepare()
        self.assertTrue(auth.verify_credentials('admin', 'admin2026'))
        self.assertFalse(auth.verify_credentials('admin', 'wrong'))
        version = auth.session_version()
        self.assertEqual(auth.prepare(), secret)
        self.assertEqual(auth.session_version(), version)
        auth.update_settings('admin', 'newpassword', 'http://192.0.2.1:8088')
        self.assertNotEqual(auth.session_version(), version)
        self.assertTrue(auth.verify_credentials('admin', 'newpassword'))
        auth.prepare()
        self.assertEqual(auth.settings()['public_base'], 'http://192.0.2.1:8088')

    def test_legacy_migration_keeps_existing_credentials(self):
        auth.SETTINGS.write_text(json.dumps({'username': 'existing', 'public_base': 'https://img.example.cn'}))
        (self.root / 'dav.htpasswd').write_text('existing:' + apr_md5_crypt.hash('oldpassword') + '\n')
        auth.prepare()
        self.assertTrue(auth.verify_credentials('existing', 'oldpassword'))
        self.assertFalse(auth.verify_credentials('admin', 'admin2026'))
        self.assertEqual(auth.settings()['public_base'], 'https://img.example.cn')


if __name__ == '__main__':
    unittest.main()
