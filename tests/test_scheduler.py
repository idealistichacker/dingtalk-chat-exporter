import os
import tempfile
import unittest
from unittest import mock

import config
import scheduler


class FakeConnection:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class SyncReservationTests(unittest.TestCase):
    def test_pre_reserved_manual_sync_runs_and_releases_state(self):
        original_state = scheduler._sync_state.copy()
        try:
            with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
                config, "SYNC_STATE_FILE", os.path.join(temp_dir, "sync.json")
            ), mock.patch.object(
                scheduler, "sync_decrypt", return_value="decrypted.db"
            ) as decrypt_db, mock.patch.object(
                scheduler, "get_connection", return_value=FakeConnection()
            ), mock.patch.object(
                scheduler, "export_incremental", return_value=None
            ), mock.patch.object(
                scheduler, "get_latest_message_time", return_value=200
            ):
                scheduler._sync_state = {
                    "last_sync_time": 100,
                    "last_sync_time_str": "old",
                    "last_export_path": None,
                    "sync_count": 0,
                    "is_syncing": False,
                    "last_error": "old error",
                    "next_sync_time": None,
                    "operation": None,
                }
                self.assertTrue(scheduler.begin_external_operation("manual_sync"))
                self.assertTrue(scheduler.do_sync(reserved=True))

                decrypt_db.assert_called_once_with()
                self.assertFalse(scheduler._sync_state["is_syncing"])
                self.assertIsNone(scheduler._sync_state["last_error"])
                self.assertIsNone(scheduler._sync_state["operation"])
                self.assertEqual(scheduler._sync_state["last_sync_time"], 200)
                self.assertEqual(scheduler._sync_state["sync_count"], 1)
        finally:
            scheduler._sync_state = original_state


if __name__ == "__main__":
    unittest.main()
