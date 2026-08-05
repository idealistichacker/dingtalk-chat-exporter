import os
import base64
import json
import sqlite3
import tempfile
import unittest
from unittest import mock
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

import config
import decrypt
import parser
import account_manager


def create_database(path, include_conversations=True):
    conn = sqlite3.connect(path)
    if include_conversations:
        conn.execute("CREATE TABLE tbconversation (cid TEXT)")
    else:
        conn.execute("CREATE TABLE unrelated (id INTEGER)")
    conn.commit()
    conn.close()


class DatabaseValidationTests(unittest.TestCase):
    def test_valid_database_is_accepted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "valid.db")
            create_database(path)
            self.assertTrue(decrypt.validate_decrypted_database(path))

    def test_empty_or_wrong_schema_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            empty_path = os.path.join(temp_dir, "empty.db")
            open(empty_path, "wb").close()
            with self.assertRaisesRegex(RuntimeError, "not created"):
                decrypt.validate_decrypted_database(empty_path)

            wrong_path = os.path.join(temp_dir, "wrong.db")
            create_database(wrong_path, include_conversations=False)
            with self.assertRaisesRegex(RuntimeError, "tbconversation"):
                decrypt.validate_decrypted_database(wrong_path)

    def test_parser_does_not_create_a_missing_database(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "missing.db")
            with self.assertRaisesRegex(RuntimeError, "unavailable"):
                parser.get_connection(path)
            self.assertFalse(os.path.exists(path))


class FailedProcess:
    def __init__(self, cmd, stdout, **kwargs):
        stdout.write("FATAL: decryption failed: invalid database (wrong key?)")
        stdout.flush()
        self.returncode = 1

    def poll(self):
        return self.returncode

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


class SuccessfulProcess:
    def __init__(self, cmd, stdout, **kwargs):
        output = cmd[cmd.index("-o") + 1]
        create_database(output)
        conn = sqlite3.connect(output)
        conn.execute("CREATE TABLE completed (value TEXT)")
        conn.commit()
        conn.close()
        stdout.write("INFO: decryption complete")
        stdout.flush()
        self.returncode = 0

    def poll(self):
        return self.returncode

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


class DecryptionReplacementTests(unittest.TestCase):
    def test_failed_decryption_preserves_existing_database(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            encrypted = os.path.join(temp_dir, "encrypted.db")
            executable = os.path.join(temp_dir, "dingwave.exe")
            output = os.path.join(temp_dir, "decrypted.db")
            open(encrypted, "wb").close()
            open(executable, "wb").close()
            create_database(output)
            with open(output, "rb") as stream:
                original = stream.read()

            with mock.patch.object(config, "DINGWAVE_PATH", executable), mock.patch(
                "decrypt.subprocess.Popen", FailedProcess
            ):
                with self.assertRaisesRegex(RuntimeError, "wrong key"):
                    decrypt.decrypt_database(encrypted, output)

            with open(output, "rb") as stream:
                self.assertEqual(stream.read(), original)
            self.assertTrue(decrypt.validate_decrypted_database(output))

    def test_successful_decryption_atomically_replaces_database(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            encrypted = os.path.join(temp_dir, "encrypted.db")
            executable = os.path.join(temp_dir, "dingwave.exe")
            output = os.path.join(temp_dir, "decrypted.db")
            open(encrypted, "wb").close()
            open(executable, "wb").close()
            create_database(output)

            with mock.patch.object(config, "DINGWAVE_PATH", executable), mock.patch(
                "decrypt.subprocess.Popen", SuccessfulProcess
            ):
                self.assertEqual(decrypt.decrypt_database(encrypted, output), output)

            conn = sqlite3.connect(output)
            try:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
            finally:
                conn.close()
            self.assertIn("completed", tables)

    def test_v3_database_is_decrypted_natively(self):
        with tempfile.TemporaryDirectory(suffix="_v3") as data_dir:
            encrypted = os.path.join(data_dir, "encrypted.db")
            plain = os.path.join(data_dir, "plain.db")
            output = os.path.join(data_dir, "output.db")
            uid = "1234567890"
            salt = "0123456789abcdef0123456789abcdef"
            payload = base64.b64encode(
                json.dumps({"salt": salt, "salt_md5": "unused"}).encode("utf-8")
            )
            with open(os.path.join(data_dir, "user_config"), "wb") as stream:
                stream.write(payload)
            create_database(plain)

            key = decrypt._generate_v3_key(uid, data_dir)
            encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
            with open(plain, "rb") as source, open(encrypted, "wb") as target:
                raw = source.read()
                self.assertEqual(len(raw) % 16, 0)
                target.write(encryptor.update(raw) + encryptor.finalize())

            self.assertEqual(
                decrypt.decrypt_database(
                    encrypted,
                    output,
                    user_uid=uid,
                    data_dir=data_dir,
                ),
                output,
            )
            self.assertTrue(decrypt.validate_decrypted_database(output))

    def test_v3_wal_frames_are_decrypted_and_checkpointed(self):
        with tempfile.TemporaryDirectory(suffix="_v3") as data_dir:
            plain = os.path.join(data_dir, "plain.db")
            encrypted = os.path.join(data_dir, "encrypted.db")
            output = os.path.join(data_dir, "output.db")
            uid = "987654321"
            payload = base64.b64encode(
                json.dumps(
                    {"salt": "0123456789abcdef0123456789abcdef"}
                ).encode("utf-8")
            )
            with open(os.path.join(data_dir, "user_config"), "wb") as stream:
                stream.write(payload)

            conn = sqlite3.connect(plain)
            try:
                conn.execute("PRAGMA page_size=4096")
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA wal_autocheckpoint=0")
                conn.execute("CREATE TABLE tbconversation (cid TEXT)")
                conn.commit()
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.execute("INSERT INTO tbconversation VALUES ('from-wal')")
                conn.commit()

                key = decrypt._generate_v3_key(uid, data_dir)
                encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
                with open(plain, "rb") as source, open(encrypted, "wb") as target:
                    raw = source.read()
                    target.write(encryptor.update(raw) + encryptor.finalize())

                wal_path = plain + "-wal"
                self.assertTrue(os.path.getsize(wal_path) > decrypt.WAL_HEADER_SIZE)
                encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
                with open(wal_path, "rb") as source, open(
                    encrypted + "-wal", "wb"
                ) as target:
                    header = source.read(decrypt.WAL_HEADER_SIZE)
                    target.write(header)
                    page_size = int.from_bytes(header[8:12], "big")
                    while True:
                        frame_header = source.read(decrypt.WAL_FRAME_HEADER_SIZE)
                        if not frame_header:
                            break
                        page = source.read(page_size)
                        target.write(frame_header)
                        target.write(encryptor.update(page))
                    encryptor.finalize()
            finally:
                conn.close()

            decrypt.decrypt_database(
                encrypted,
                output,
                user_uid=uid,
                data_dir=data_dir,
            )
            result = sqlite3.connect(output)
            try:
                rows = result.execute("SELECT cid FROM tbconversation").fetchall()
            finally:
                result.close()
            self.assertEqual(rows, [("from-wal",)])


class AccountDetectionTests(unittest.TestCase):
    def test_v3_uid_discovery_accepts_nine_digits_and_prioritizes_real_uid(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            log_dir = os.path.join(temp_dir, "log")
            os.makedirs(log_dir)
            with open(
                os.path.join(log_dir, "cef_debug.log.2026-08-05"),
                "w",
                encoding="utf-8",
            ) as stream:
                stream.write("&uid=12345678901\n" * 20)
                stream.write("real_uid=987654321\n")
                stream.write("myOpenId=987654321\n")

            candidates = config._discover_v3_uid_candidates(temp_dir)

        self.assertEqual(candidates[:2], ["987654321", "12345678901"])

    def test_startup_tries_v3_uid_candidates_until_decryption_succeeds(self):
        data_dir = r"C:\DingTalk\folder-hash_v3"
        v3_account = {
            "uid": "folder-hash",
            "data_dir": data_dir,
            "version": "v3",
            "supported": True,
            "last_modified": 1,
            "decrypt_uid_candidates": ["wrong-user", "1234567890"],
        }
        with mock.patch.object(
            decrypt, "validate_decrypted_database", side_effect=RuntimeError("missing")
        ), mock.patch.object(
            config, "discover_dingtalk_accounts", return_value=[v3_account]
        ), mock.patch.object(config, "DINGTALK_DATA_DIR", data_dir), mock.patch.object(
            config, "USER_UID", "folder-hash"
        ), mock.patch.object(
            decrypt,
            "sync_decrypt",
            side_effect=[RuntimeError("wrong key"), "decrypted.db"],
        ) as sync, mock.patch.object(config, "configure_account") as configure, mock.patch.object(
            config, "remember_account_uid"
        ) as remember:
            result = decrypt.ensure_decrypted_database()

        self.assertEqual(result, "decrypted.db")
        self.assertEqual(
            [call.kwargs["user_uid"] for call in sync.call_args_list],
            ["wrong-user", "1234567890"],
        )
        configure.assert_called_once_with("1234567890", data_dir, persist=True)
        remember.assert_called_once_with(data_dir, "1234567890")

    def test_auto_detection_includes_v3_directories(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = os.path.join(temp_dir, "DingTalk")
            v2 = os.path.join(base, "supported_v2", "DBFiles")
            v3 = os.path.join(base, "newer_v3", "DBFiles")
            os.makedirs(v2)
            os.makedirs(v3)
            open(os.path.join(v2, "dingtalk.db"), "wb").close()
            open(os.path.join(v3, "dingtalk.db"), "wb").close()
            os.utime(os.path.join(v3, "dingtalk.db"), (2_000_000_000,) * 2)

            env = {
                "APPDATA": temp_dir,
                "LOCALAPPDATA": "",
                "USERPROFILE": "",
            }
            with mock.patch.dict(os.environ, env, clear=True):
                path, uid = config._detect_dingtalk_user()
                accounts = config.discover_dingtalk_accounts()

            self.assertEqual(uid, "newer")
            self.assertEqual(path, os.path.dirname(v3))

            by_uid = {account["uid"]: account for account in accounts}
            self.assertTrue(by_uid["supported"]["supported"])
            self.assertTrue(by_uid["newer"]["supported"])

    def test_v3_account_tries_uid_candidates_until_decryption_succeeds(self):
        v3_account = {
            "uid": "v3-user",
            "data_dir": r"C:\DingTalk\v3-user_v3",
            "version": "v3",
            "supported": True,
            "last_modified": 1,
            "decrypt_uid_candidates": ["wrong-user", "1234567890"],
        }
        with mock.patch.object(
            config, "discover_dingtalk_accounts", return_value=[v3_account]
        ), mock.patch.object(config, "DINGTALK_DATA_DIR", r"C:\DingTalk\old_v2"), mock.patch(
            "account_manager.sync_decrypt",
            side_effect=[RuntimeError("wrong key"), "decrypted.db"],
        ) as sync, mock.patch.object(config, "configure_account") as configure, mock.patch.object(
            config, "remember_account_uid"
        ) as remember:
            selected = account_manager.select_account("v3-user")

        self.assertEqual(selected, v3_account)
        self.assertEqual(sync.call_count, 2)
        configure.assert_called_once_with(
            "1234567890", v3_account["data_dir"], persist=True
        )
        remember.assert_called_once_with(v3_account["data_dir"], "1234567890")

    def test_supported_account_is_decrypted_before_activation(self):
        v2_account = {
            "uid": "new-user",
            "data_dir": r"C:\DingTalk\new-user_v2",
            "version": "v2",
            "supported": True,
            "last_modified": 2,
        }
        with mock.patch.object(
            config, "discover_dingtalk_accounts", return_value=[v2_account]
        ), mock.patch.object(config, "USER_UID", "old-user"), mock.patch.object(
            config, "DINGTALK_DATA_DIR", r"C:\DingTalk\old-user_v2"
        ), mock.patch("account_manager.sync_decrypt") as sync, mock.patch.object(
            config, "configure_account"
        ) as configure:
            selected = account_manager.select_account("new-user")

        self.assertEqual(selected, v2_account)
        sync.assert_called_once_with(
            data_dir=v2_account["data_dir"], user_uid=v2_account["uid"]
        )
        configure.assert_called_once_with(
            v2_account["uid"], v2_account["data_dir"], persist=True
        )

    def test_same_directory_is_redecrypted_when_database_owner_does_not_match(self):
        account = {
            "uid": "folder-hash",
            "data_dir": r"C:\DingTalk\folder-hash_v3",
            "version": "v3",
            "supported": True,
            "last_modified": 2,
            "decrypt_uid_candidates": ["123456789"],
        }
        with mock.patch.object(
            config, "discover_dingtalk_accounts", return_value=[account]
        ), mock.patch.object(config, "USER_UID", "123456789"), mock.patch.object(
            config, "DINGTALK_DATA_DIR", account["data_dir"]
        ), mock.patch(
            "account_manager._decrypted_account_matches", return_value=False
        ), mock.patch(
            "account_manager.sync_decrypt", return_value="decrypted.db"
        ) as sync, mock.patch.object(config, "configure_account"), mock.patch.object(
            config, "remember_account_uid"
        ):
            account_manager.select_account(account["uid"])

        sync.assert_called_once_with(
            data_dir=account["data_dir"], user_uid="123456789"
        )

    def test_decrypted_database_owner_must_match_active_account(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            marker = os.path.join(temp_dir, "account.json")
            with mock.patch.object(config, "DECRYPTED_ACCOUNT_FILE", marker), mock.patch.object(
                config, "DECRYPTED_DIR", temp_dir
            ):
                decrypt._write_decrypted_account("user-a", r"C:\DingTalk\user-a_v2")
                self.assertTrue(
                    decrypt._decrypted_account_matches(
                        "user-a", r"C:\DingTalk\user-a_v2"
                    )
                )
                self.assertFalse(
                    decrypt._decrypted_account_matches(
                        "user-b", r"C:\DingTalk\user-b_v2"
                    )
                )


if __name__ == "__main__":
    unittest.main()
