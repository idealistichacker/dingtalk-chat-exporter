import os
import shutil
import subprocess
import logging
import time
import tempfile
import sqlite3
import json
import base64
import hashlib
import struct

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

import config

logger = logging.getLogger(__name__)


REQUIRED_TABLES = {"tbconversation"}
PAGE_SIZE = 4096
WAL_HEADER_SIZE = 32
WAL_FRAME_HEADER_SIZE = 24
V3_PBKDF2_SALT = b"666DingTalk888"[:8]
V3_PBKDF2_ITERATIONS = 1000
V3_PBKDF2_KEYLEN = 32


def validate_decrypted_database(db_path):
    """Validate that a file is a readable DingTalk database."""
    if not os.path.isfile(db_path) or os.path.getsize(db_path) == 0:
        raise RuntimeError(f"Decrypted database was not created: {db_path}")

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            integrity = conn.execute("PRAGMA quick_check").fetchone()
            if not integrity or integrity[0] != "ok":
                raise RuntimeError(f"SQLite integrity check failed: {integrity}")
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        raise RuntimeError(f"Output is not a valid SQLite database: {exc}") from exc

    missing = REQUIRED_TABLES - tables
    if missing:
        raise RuntimeError(
            "Decrypted database has an unsupported schema; missing table(s): "
            + ", ".join(sorted(missing))
        )
    return True


def _database_snapshot(db_path):
    """Return size/mtime state for a SQLite database and its sidecar files."""
    snapshot = []
    for suffix in ("", "-wal", "-shm"):
        path = db_path + suffix
        if os.path.exists(path):
            stat = os.stat(path)
            snapshot.append((suffix, stat.st_size, stat.st_mtime_ns))
    return tuple(snapshot)


def _checkpoint_database(db_path):
    """Move committed WAL data into the main DB before publishing it."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    finally:
        conn.close()


def _load_v3_salt(data_dir):
    config_path = os.path.join(data_dir, "user_config")
    if not os.path.isfile(config_path):
        raise FileNotFoundError(f"DingTalk V3 user_config not found: {config_path}")
    try:
        with open(config_path, "r", encoding="utf-8") as stream:
            payload = base64.b64decode(stream.read().strip()).decode("utf-8")
        salt = str(json.loads(payload).get("salt", "")).strip()
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot parse DingTalk V3 user_config: {exc}") from exc
    if not salt:
        raise RuntimeError("DingTalk V3 user_config does not contain a salt")
    return salt


def _generate_v3_key(user_uid, data_dir):
    salt = _load_v3_salt(data_dir)
    derived = hashlib.pbkdf2_hmac(
        "sha1",
        (str(user_uid) + salt).encode("utf-8"),
        V3_PBKDF2_SALT,
        V3_PBKDF2_ITERATIONS,
        V3_PBKDF2_KEYLEN,
    )
    return hashlib.md5(derived).hexdigest()[:16].encode("ascii")


def _decrypt_ecb_bytes(data, decryptor):
    if len(data) % 16:
        raise RuntimeError("Encrypted V3 page is not aligned to the AES block size")
    return decryptor.update(data)


def _decrypt_v3_database_pages(input_path, output_path, key):
    decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    with open(input_path, "rb") as source, open(output_path, "wb") as target:
        while True:
            page = source.read(PAGE_SIZE)
            if not page:
                break
            target.write(
                _decrypt_ecb_bytes(page, decryptor) if len(page) == PAGE_SIZE else page
            )
        decryptor.finalize()


def _wal_checksum(data, byteorder, s0=0, s1=0):
    if len(data) % 8:
        raise RuntimeError("WAL checksum input is not aligned to 8 bytes")
    for offset in range(0, len(data), 8):
        x0 = int.from_bytes(data[offset:offset + 4], byteorder)
        x1 = int.from_bytes(data[offset + 4:offset + 8], byteorder)
        s0 = (s0 + x0 + s1) & 0xFFFFFFFF
        s1 = (s1 + x1 + s0) & 0xFFFFFFFF
    return s0, s1


def _decrypt_v3_wal(input_path, output_path, key):
    decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    with open(input_path, "rb") as source, open(output_path, "wb") as target:
        header = source.read(WAL_HEADER_SIZE)
        if len(header) != WAL_HEADER_SIZE:
            raise RuntimeError("Encrypted V3 WAL has an invalid header")
        magic = int.from_bytes(header[:4], "big")
        if magic == 0x377F0682:
            byteorder = "little"
        elif magic == 0x377F0683:
            byteorder = "big"
        else:
            raise RuntimeError(f"Unsupported WAL magic number: 0x{magic:08x}")
        page_size = int.from_bytes(header[8:12], "big")
        page_size = 65536 if page_size == 1 else page_size
        checksum_1, checksum_2 = _wal_checksum(header[:24], byteorder)
        target.write(header)

        frames = 0
        while True:
            frame_header = bytearray(source.read(WAL_FRAME_HEADER_SIZE))
            if not frame_header:
                break
            if len(frame_header) != WAL_FRAME_HEADER_SIZE:
                logger.warning(
                    "Ignoring an incomplete V3 WAL frame header copied during an active write"
                )
                break
            page = source.read(page_size)
            if len(page) != page_size:
                logger.warning(
                    "Ignoring an incomplete V3 WAL page copied during an active write"
                )
                break
            page = _decrypt_ecb_bytes(page, decryptor)
            checksum_1, checksum_2 = _wal_checksum(
                bytes(frame_header[:8]) + page,
                byteorder,
                checksum_1,
                checksum_2,
            )
            frame_header[16:20] = struct.pack(">I", checksum_1)
            frame_header[20:24] = struct.pack(">I", checksum_2)
            target.write(frame_header)
            target.write(page)
            frames += 1
        decryptor.finalize()
    return frames


def copy_encrypted_db(encrypted_db=None, retry_count=None, retry_delay=None):
    """Copy encrypted database files to a temp directory to avoid lock conflicts."""
    if retry_count is None:
        retry_count = config.COPY_RETRY_COUNT
    if retry_delay is None:
        retry_delay = config.COPY_RETRY_DELAY

    source_db = encrypted_db or config.ENCRYPTED_DB
    if not os.path.isfile(source_db):
        raise FileNotFoundError(
            f"DingTalk database not found: {source_db}. "
            "Make sure the desktop client has been opened and a supported *_v2 or *_v3 "
            "account directory is selected."
        )

    temp_dir = tempfile.mkdtemp(prefix="dingtalk_encrypt_", dir=config.DECRYPTED_DIR)
    dest_db = os.path.join(temp_dir, "dingtalk_encrypted.db")

    # Files to copy: main db + wal + shm
    files_to_copy = [
        (source_db, dest_db),
        (source_db + "-wal", dest_db + "-wal"),
        (source_db + "-shm", dest_db + "-shm"),
    ]

    for attempt in range(retry_count):
        try:
            for src, dst in files_to_copy:
                if os.path.exists(src):
                    shutil.copy2(src, dst)
            if not os.path.isfile(dest_db):
                raise FileNotFoundError(f"Database copy was not created: {dest_db}")
            logger.info(f"Successfully copied encrypted database to {temp_dir}")
            return dest_db
        except (PermissionError, OSError) as e:
            logger.warning(f"Copy attempt {attempt + 1}/{retry_count} failed: {e}")
            if attempt < retry_count - 1:
                time.sleep(retry_delay)
            else:
                # Clean up partial copy
                shutil.rmtree(temp_dir, ignore_errors=True)
                raise RuntimeError(
                    f"Failed to copy encrypted database after {retry_count} attempts: {e}"
                )

    shutil.rmtree(temp_dir, ignore_errors=True)
    raise RuntimeError("Failed to copy encrypted database")


def decrypt_database(
    encrypted_db_path=None,
    output_path=None,
    user_uid=None,
    encrypted_db_source=None,
    data_dir=None,
):
    """Decrypt a V2 database with dingwave or a V3 database natively."""
    selected_data_dir = os.path.normpath(data_dir or config.DINGTALK_DATA_DIR)
    is_v3 = selected_data_dir.lower().endswith("_v3")

    if not is_v3 and not os.path.isfile(config.DINGWAVE_PATH):
        raise FileNotFoundError(
            f"dingwave binary not found at: {config.DINGWAVE_PATH}\n"
            f"Please download it from https://github.com/p1g3/dingwave/releases\n"
            f"and place it in the tools/ directory.\n"
            f"Expected: tools/dingwave.exe (Windows) or tools/dingwave (Linux/Mac)"
        )

    owns_encrypted_copy = encrypted_db_path is None
    if owns_encrypted_copy:
        encrypted_db_path = copy_encrypted_db(encrypted_db_source)
    if output_path is None:
        output_path = config.DECRYPTED_DB_PATH

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    fd, candidate_path = tempfile.mkstemp(
        prefix="dingtalk_decrypted_", suffix=".db", dir=os.path.dirname(output_path)
    )
    os.close(fd)
    os.unlink(candidate_path)

    logger.info(f"Starting decryption: {encrypted_db_path} -> {candidate_path}")

    cmd = [
        config.DINGWAVE_PATH,
        "-d", encrypted_db_path,
        "-k", str(user_uid or config.USER_UID),
        "-o", candidate_path,
    ]

    proc = None
    process_output = ""
    validation_error = None
    try:
        if is_v3:
            key = _generate_v3_key(user_uid or config.USER_UID, selected_data_dir)
            _decrypt_v3_database_pages(encrypted_db_path, candidate_path, key)
            encrypted_wal = encrypted_db_path + "-wal"
            if os.path.isfile(encrypted_wal) and os.path.getsize(encrypted_wal) > 0:
                frames = _decrypt_v3_wal(encrypted_wal, candidate_path + "-wal", key)
                logger.info(f"Decrypted DingTalk V3 WAL frames: {frames}")
            process_output = "native DingTalk V3 decryption"
        else:
            # dingwave decrypts the DB then starts a web server and never exits.
            with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as log_file:
                proc = subprocess.Popen(
                    cmd,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    text=True,
                )

                deadline = time.monotonic() + 300
                previous_snapshot = None
                stable_checks = 0
                while time.monotonic() < deadline:
                    if proc.poll() is not None:
                        break
                    if os.path.exists(candidate_path):
                        snapshot = _database_snapshot(candidate_path)
                        if snapshot == previous_snapshot:
                            stable_checks += 1
                        else:
                            previous_snapshot = snapshot
                            stable_checks = 0
                        if stable_checks >= 2:
                            try:
                                validate_decrypted_database(candidate_path)
                                break
                            except RuntimeError as exc:
                                validation_error = exc
                    time.sleep(1)
                else:
                    raise RuntimeError("Decryption timed out after 300 seconds")

                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                    logger.info("dingwave process terminated after decryption")

                log_file.seek(0)
                process_output = log_file.read().strip()

        for line in process_output.splitlines():
            if line.strip():
                logger.info(f"decryption: {line.strip()}")

        try:
            _checkpoint_database(candidate_path)
            validate_decrypted_database(candidate_path)
        except (RuntimeError, sqlite3.DatabaseError) as exc:
            detail = str(validation_error or exc)
            if not is_v3 and process_output:
                detail = f"{detail}; decryptor output: {process_output}"
            raise RuntimeError(f"database decryption failed: {detail}") from exc

        os.replace(candidate_path, output_path)
        output_size = os.path.getsize(output_path)
        logger.info(f"Decryption complete. Output size: {output_size / 1024 / 1024:.1f} MB")

        return output_path

    except Exception:
        # Make sure process is dead on error
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait()
        raise
    finally:
        for suffix in ("", "-wal", "-shm"):
            candidate_file = candidate_path + suffix
            if os.path.exists(candidate_file):
                os.remove(candidate_file)
        # Clean up the encrypted copy
        if owns_encrypted_copy and encrypted_db_path:
            temp_dir = os.path.dirname(encrypted_db_path)
            shutil.rmtree(temp_dir, ignore_errors=True)
            logger.info(f"Cleaned up temp directory: {temp_dir}")


def _write_decrypted_account(uid, data_dir):
    os.makedirs(config.DECRYPTED_DIR, exist_ok=True)
    temp_path = config.DECRYPTED_ACCOUNT_FILE + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as stream:
        json.dump(
            {"uid": str(uid), "data_dir": os.path.normpath(data_dir)},
            stream,
            ensure_ascii=False,
            indent=2,
        )
    os.replace(temp_path, config.DECRYPTED_ACCOUNT_FILE)


def _decrypted_account_matches(uid, data_dir):
    try:
        with open(config.DECRYPTED_ACCOUNT_FILE, "r", encoding="utf-8") as stream:
            owner = json.load(stream)
        return (
            str(owner.get("uid", "")) == str(uid)
            and os.path.normcase(os.path.normpath(owner.get("data_dir", "")))
            == os.path.normcase(os.path.normpath(data_dir))
        )
    except (OSError, json.JSONDecodeError, AttributeError):
        return False


def sync_decrypt(data_dir=None, user_uid=None):
    """Full sync: copy encrypted DB, decrypt, return path to decrypted DB."""
    selected_dir = os.path.normpath(data_dir or config.DINGTALK_DATA_DIR)
    selected_uid = str(user_uid or config.USER_UID)
    encrypted_source = os.path.join(selected_dir, "DBFiles", "dingtalk.db")
    logger.info("=== Starting sync decrypt ===")
    decrypted_path = decrypt_database(
        user_uid=selected_uid,
        encrypted_db_source=encrypted_source,
        data_dir=selected_dir,
    )
    _write_decrypted_account(selected_uid, selected_dir)
    logger.info("=== Sync decrypt complete ===")
    return decrypted_path


def ensure_decrypted_database():
    """Return a valid decrypted DB, creating it on first launch if needed."""
    try:
        validate_decrypted_database(config.DECRYPTED_DB_PATH)
        if _decrypted_account_matches(config.USER_UID, config.DINGTALK_DATA_DIR):
            return config.DECRYPTED_DB_PATH
        logger.info("Decrypted database belongs to a different DingTalk account")
    except RuntimeError:
        pass
    logger.info("No valid decrypted database found for the active account; decrypting")

    selected_path = os.path.normcase(os.path.normpath(config.DINGTALK_DATA_DIR))
    account = next(
        (
            item
            for item in config.discover_dingtalk_accounts()
            if os.path.normcase(os.path.normpath(item["data_dir"])) == selected_path
        ),
        None,
    )
    candidates = (
        account.get("decrypt_uid_candidates", [config.USER_UID])
        if account
        else [config.USER_UID]
    )
    last_error = None
    for candidate_uid in candidates:
        try:
            path = sync_decrypt(user_uid=candidate_uid)
            config.configure_account(candidate_uid, config.DINGTALK_DATA_DIR, persist=True)
            config.remember_account_uid(config.DINGTALK_DATA_DIR, candidate_uid)
            return path
        except RuntimeError as exc:
            last_error = exc
            if not config.DINGTALK_DATA_DIR.lower().endswith("_v3"):
                break

    raise RuntimeError(
        "Cannot decrypt the selected DingTalk account with any UID found in local logs"
        if config.DINGTALK_DATA_DIR.lower().endswith("_v3")
        else str(last_error or "DingTalk database decryption failed")
    ) from last_error


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    path = sync_decrypt()
    print(f"Decrypted database: {path}")
