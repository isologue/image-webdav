import os
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import cleanup


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.old_data, cleanup.DATA = cleanup.DATA, self.root / 'images'
        self.old_state, cleanup.STATE_FILE = cleanup.STATE_FILE, self.root / 'config.json'
        self.addCleanup(self.restore_paths)
        cleanup.DATA.mkdir()

    def restore_paths(self):
        cleanup.DATA = self.old_data
        cleanup.STATE_FILE = self.old_state

    def file_at(self, relative, age_hours, content=b'1234'):
        path = cleanup.DATA / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        modified = time.time() - age_hours * 3600
        os.utime(path, (modified, modified))
        return path

    def test_expired_files_only_and_empty_dirs(self):
        old = self.file_at('2025/01/01/old.png', 4)
        recent = self.file_at('2026/10/07/new.png', 0.5)
        result = cleanup.remove_expired(2)
        self.assertEqual(result, {'deleted': 1, 'freed_bytes': 4, 'failed': 0})
        self.assertFalse(old.exists())
        self.assertFalse(old.parent.exists())
        self.assertTrue(recent.exists())

    def test_cutoff_is_strict_and_does_not_touch_settings(self):
        now = time.time()
        older = self.file_at('older.png', 4)
        at_cutoff = self.file_at('at-cutoff.png', 4)
        newer = self.file_at('newer.png', 4)
        os.utime(older, (now - 4 * 3600 - 1, now - 4 * 3600 - 1))
        os.utime(at_cutoff, (now - 4 * 3600, now - 4 * 3600))
        os.utime(newer, (now - 4 * 3600 + 1, now - 4 * 3600 + 1))
        cleanup.STATE_FILE.write_text('settings remain', encoding='utf-8')
        result = cleanup.remove_expired(4, now=now)
        self.assertEqual(result['deleted'], 1)
        self.assertFalse(older.exists())
        self.assertTrue(at_cutoff.exists())
        self.assertTrue(newer.exists())
        self.assertEqual(cleanup.STATE_FILE.read_text(encoding='utf-8'), 'settings remain')

    def test_does_not_follow_symlinks_or_touch_config(self):
        outside = self.root / 'config.json'
        outside.write_text('keep', encoding='utf-8')
        old = time.time() - 10 * 3600
        os.utime(outside, (old, old))
        try:
            (cleanup.DATA / 'outside').symlink_to(self.root, target_is_directory=True)
            (cleanup.DATA / 'linked.png').symlink_to(outside)
        except OSError:
            self.skipTest('Symlinks are unavailable on this platform')
        result = cleanup.remove_expired(1)
        self.assertEqual(result['deleted'], 0)
        self.assertEqual(outside.read_text(encoding='utf-8'), 'keep')

    def test_manual_and_scheduled_state(self):
        old = self.file_at('old.png', 4)
        recent = self.file_at('new.png', 0.5)
        self.assertFalse(cleanup.get_state()['enabled'])
        state = cleanup.set_policy(True, 2, 1)
        self.assertGreater(datetime.fromisoformat(state['next_run_at']).timestamp(), time.time())
        self.assertEqual(cleanup.get_state()['next_run_at'], state['next_run_at'])
        cleanup.run_due_cleanup()
        self.assertTrue(old.exists())
        result = cleanup.run_cleanup(2)
        self.assertEqual(result['deleted'], 1)
        self.assertTrue(recent.exists())
        self.assertEqual(cleanup.get_state()['last_result']['trigger'], 'manual')
        other = self.file_at('another.png', 5)
        state = cleanup.get_state()
        state['next_run_at'] = '2000-01-01T00:00:00+00:00'
        cleanup._write_state(state)
        cleanup.run_due_cleanup()
        self.assertFalse(other.exists())
        state = cleanup.get_state()
        self.assertEqual(state['last_result']['trigger'], 'scheduled')
        self.assertGreater(datetime.fromisoformat(state['next_run_at']).timestamp(), time.time())
        cleanup.run_due_cleanup()
        self.assertEqual(cleanup.get_state()['last_result'], state['last_result'])
        cleanup.set_policy(False, 2, 1)
        old_again = self.file_at('old-again.png', 5)
        cleanup.run_due_cleanup()
        self.assertTrue(old_again.exists())

    def test_policy_changed_during_cleanup_keeps_new_schedule(self):
        cleanup.set_policy(True, 2, 1)
        state = cleanup.get_state()
        state['next_run_at'] = '2000-01-01T00:00:00+00:00'
        cleanup._write_state(state)

        def change_policy(_hours):
            cleanup.set_policy(True, 48, 2)
            return {'deleted': 0, 'freed_bytes': 0, 'failed': 0}

        with patch.object(cleanup, 'remove_expired', side_effect=change_policy):
            cleanup.run_due_cleanup()
        state = cleanup.get_state()
        self.assertEqual(state['retention_hours'], 48)
        self.assertGreater(datetime.fromisoformat(state['next_run_at']).timestamp(), time.time() + 7000)


if __name__ == '__main__':
    unittest.main()
