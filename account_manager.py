import os
import sqlite3
import threading

import config
from decrypt import (
    _decrypted_account_matches,
    sync_decrypt,
    validate_decrypted_database,
)


_switch_lock = threading.Lock()


def _active_profile_name():
    try:
        validate_decrypted_database(config.DECRYPTED_DB_PATH)
        conn = sqlite3.connect(f"file:{config.DECRYPTED_DB_PATH}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT nick, realName, alias FROM tbuser_profile_v2 "
                "WHERE CAST(uid AS TEXT) = ? LIMIT 1",
                (str(config.USER_UID),),
            ).fetchone()
        finally:
            conn.close()
        if row:
            return next((value for value in row if value), None)
    except (sqlite3.DatabaseError, RuntimeError, OSError):
        pass
    return None


def list_accounts():
    accounts = config.discover_dingtalk_accounts()
    active_path = os.path.normcase(os.path.normpath(config.DINGTALK_DATA_DIR))
    active_name = _active_profile_name()
    result = []
    most_recent_uid = accounts[0]["uid"] if accounts else None
    for account in accounts:
        is_active = (
            os.path.normcase(os.path.normpath(account["data_dir"])) == active_path
        )
        display_name = active_name if is_active and active_name else None
        if not display_name and account["uid"] == most_recent_uid:
            display_name = "最近使用的账号"
        result.append({
            "uid": account["uid"],
            "version": account["version"],
            "supported": account["supported"],
            "active": is_active,
            "display_name": display_name,
            "last_modified": account["last_modified"],
            "reason": None,
        })
    return result


def resolve_account(uid, require_supported=False):
    matches = [
        account
        for account in config.discover_dingtalk_accounts()
        if account["uid"] == str(uid)
    ]
    if not matches:
        raise ValueError("未找到该钉钉账号")
    account = matches[0]
    if require_supported and not account["supported"]:
        raise ValueError(account.get("reason") or "该账号数据库版本暂不支持解密")
    return account


def select_account(uid):
    """Decrypt and activate a supported local account."""
    with _switch_lock:
        account = resolve_account(uid, require_supported=True)

        current_path = os.path.normcase(os.path.normpath(config.DINGTALK_DATA_DIR))
        selected_path = os.path.normcase(os.path.normpath(account["data_dir"]))
        if current_path == selected_path and _decrypted_account_matches(
            config.USER_UID, config.DINGTALK_DATA_DIR
        ):
            return account

        last_error = None
        resolved_uid = None
        for candidate_uid in account.get("decrypt_uid_candidates", [account["uid"]]):
            try:
                sync_decrypt(data_dir=account["data_dir"], user_uid=candidate_uid)
                resolved_uid = candidate_uid
                break
            except RuntimeError as exc:
                last_error = exc
                if account["version"] != "v3":
                    break
        if resolved_uid is None:
            raise RuntimeError(
                "无法从本地钉钉日志解析出该 V3 账号的真实 UID"
                if account["version"] == "v3"
                else str(last_error or "账号数据库解密失败")
            ) from last_error

        config.configure_account(resolved_uid, account["data_dir"], persist=True)
        config.remember_account_uid(account["data_dir"], resolved_uid)
        return account
