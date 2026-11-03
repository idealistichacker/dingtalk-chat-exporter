import os
import sys
import logging
import json
import re

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(PROJECT_DIR, "data")
SELECTED_ACCOUNT_FILE = os.path.join(DATA_DIR, "selected_account.json")
ACCOUNT_UID_CACHE_FILE = os.path.join(DATA_DIR, "account_uids.json")

logger = logging.getLogger(__name__)


def _discover_v3_uid_candidates(dingtalk_base):
    """Find likely real V3 UIDs from recent DingTalk client logs."""
    log_dir = os.path.join(dingtalk_base, "log")
    if not os.path.isdir(log_dir):
        return []
    try:
        names = os.listdir(log_dir)
    except OSError:
        return []

    selected = sorted(name for name in names if name.startswith("cef_debug.log"))[-12:]
    gaea_logs = sorted(name for name in names if name.startswith("gaea.log"))
    if gaea_logs:
        selected.append(gaea_logs[-1])

    patterns = [
        (re.compile(r"real_uid=(\d{8,14})"), 10_000),
        (re.compile(r"myOpenId=(\d{8,14})"), 10_000),
        (re.compile(r"&uid=(\d{8,14})"), 10),
        (re.compile(r"&cid=(\d{8,14}):"), 1),
    ]
    scores = {}
    for name in selected:
        path = os.path.join(log_dir, name)
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as stream:
                text = stream.read()
        except OSError:
            continue
        for pattern, weight in patterns:
            for uid in pattern.findall(text):
                scores[uid] = scores.get(uid, 0) + weight

    return [
        uid
        for uid, _ in sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:20]
    ]


def _load_account_uid_cache():
    try:
        with open(ACCOUNT_UID_CACHE_FILE, "r", encoding="utf-8") as stream:
            value = json.load(stream)
            return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def remember_account_uid(data_dir, uid):
    cache = _load_account_uid_cache()
    cache[os.path.normcase(os.path.normpath(data_dir))] = str(uid)
    os.makedirs(DATA_DIR, exist_ok=True)
    temp_path = ACCOUNT_UID_CACHE_FILE + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as stream:
        json.dump(cache, stream, ensure_ascii=False, indent=2)
    os.replace(temp_path, ACCOUNT_UID_CACHE_FILE)


def _dingtalk_search_bases():
    """Return possible DingTalk data roots without duplicates."""
    search_bases = []
    appdata = os.environ.get("APPDATA", "")
    local_appdata = os.environ.get("LOCALAPPDATA", "")
    userprofile = os.environ.get("USERPROFILE", "")

    if appdata:
        search_bases.append(os.path.join(appdata, "DingTalk"))
    if local_appdata and local_appdata != appdata:
        search_bases.append(os.path.join(local_appdata, "DingTalk"))
    if userprofile:
        search_bases.append(os.path.join(userprofile, "AppData", "Roaming", "DingTalk"))
        search_bases.append(os.path.join(userprofile, "AppData", "Local", "DingTalk"))
        search_bases.append(os.path.join(userprofile, "DingTalk"))

    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        search_bases.append(os.path.join(home, "Library", "Application Support", "DingTalk"))
        search_bases.append(os.path.join(home, "Library", "Application Support", "DingTalkMac"))
    elif sys.platform.startswith("linux"):
        search_bases.append(os.path.join(home, ".config", "DingTalk"))
        search_bases.append(os.path.join(home, ".local", "share", "DingTalk"))

    seen = set()
    unique_bases = []
    for base in search_bases:
        normalized = os.path.normcase(os.path.normpath(base))
        if normalized not in seen:
            seen.add(normalized)
            unique_bases.append(base)
    return unique_bases


def discover_dingtalk_accounts():
    """Discover all local DingTalk accounts and their compatibility."""
    accounts = []
    seen_paths = set()
    uid_cache = _load_account_uid_cache()
    for dingtalk_base in _dingtalk_search_bases():
        if not os.path.isdir(dingtalk_base):
            continue
        try:
            entries = os.listdir(dingtalk_base)
        except OSError as exc:
            logger.warning(f"Cannot scan DingTalk data directory {dingtalk_base}: {exc}")
            continue

        v3_uid_candidates = _discover_v3_uid_candidates(dingtalk_base)
        for entry in entries:
            if not entry.endswith(("_v2", "_v3")):
                continue
            full_path = os.path.join(dingtalk_base, entry)
            db_file = os.path.join(full_path, "DBFiles", "dingtalk.db")
            normalized = os.path.normcase(os.path.normpath(full_path))
            if normalized in seen_paths or not os.path.isfile(db_file):
                continue
            try:
                version = entry.rsplit("_v", 1)[1]
                cached_uid = uid_cache.get(normalized)
                decrypt_uid_candidates = []
                if cached_uid:
                    decrypt_uid_candidates.append(cached_uid)
                if version == "3":
                    decrypt_uid_candidates.extend(v3_uid_candidates)
                decrypt_uid_candidates.append(entry.rsplit("_v", 1)[0])
                decrypt_uid_candidates = list(dict.fromkeys(decrypt_uid_candidates))
                accounts.append({
                    "uid": entry.rsplit("_v", 1)[0],
                    "data_dir": full_path,
                    "version": f"v{version}",
                    "supported": True,
                    "last_modified": os.path.getmtime(db_file),
                    "decrypt_uid_candidates": decrypt_uid_candidates,
                })
                seen_paths.add(normalized)
            except OSError as exc:
                logger.warning(f"Cannot inspect DingTalk database {db_file}: {exc}")

    accounts.sort(key=lambda account: account["last_modified"], reverse=True)
    return accounts


def _load_selected_account():
    try:
        with open(SELECTED_ACCOUNT_FILE, "r", encoding="utf-8") as stream:
            value = json.load(stream)
            if isinstance(value, dict):
                return value
    except (OSError, json.JSONDecodeError):
        pass
    return None


def _detect_dingtalk_user():
    """Auto-detect DingTalk user data directory and UID.

    Scans multiple possible DingTalk data directories for supported *_v2 and *_v3 folders.
    Returns (data_dir, uid) or (None, None) if not found.

    Override with environment variables:
      DINGTALK_UID       — your DingTalk user UID
      DINGTALK_DATA_DIR  — full path to the *_v2 or *_v3 directory
    """
    # Environment variables take highest priority
    env_uid = os.environ.get("DINGTALK_UID", "").strip()
    env_dir = os.environ.get("DINGTALK_DATA_DIR", "").strip()

    if env_uid and env_dir:
        logger.info(f"Using config from environment: UID={env_uid}")
        return env_dir, env_uid

    accounts = discover_dingtalk_accounts()
    supported = [account for account in accounts if account["supported"]]

    selected = _load_selected_account()
    if selected:
        selected_path = os.path.normcase(os.path.normpath(selected.get("data_dir", "")))
        for account in supported:
            if os.path.normcase(os.path.normpath(account["data_dir"])) == selected_path:
                selected_uid = str(selected.get("uid", "")) or account["uid"]
                logger.info(f"Using saved DingTalk account: UID={selected_uid}")
                return account["data_dir"], selected_uid

    if not supported:
        return None, None

    if len(supported) == 1:
        account = supported[0]
        logger.info(f"Auto-detected DingTalk user: UID={account['uid']}")
        return account["data_dir"], account["uid"]

    # Multiple users: pick the one with most recently modified database
    account = supported[0]
    all_uids = [f"  UID={item['uid']} (path={item['data_dir']})" for item in supported]
    logger.warning(
        f"Multiple DingTalk users found, using the most recent (UID={account['uid']}):\n"
        + "\n".join(all_uids)
        + "\nUse the account selector in the web interface to switch users."
    )
    return account["data_dir"], account["uid"]


def _detect_dingwave():
    """Auto-detect the dingwave binary in the tools/ directory.

    Checks multiple possible filenames to handle:
    - Platform differences (dingwave.exe vs dingwave)
    - Users who rename or download with different names
    """
    tools_dir = os.path.join(PROJECT_DIR, "tools")
    candidates = []

    if sys.platform == "win32":
        candidates = ["dingwave.exe", "dingwave"]
    else:
        candidates = ["dingwave", "dingwave.exe"]

    # Check exact names first
    for name in candidates:
        full = os.path.join(tools_dir, name)
        if os.path.isfile(full):
            return full

    # Fallback: find any executable-like file in tools/ with 'dingwave' in name
    if os.path.isdir(tools_dir):
        for f in os.listdir(tools_dir):
            lower = f.lower()
            if "dingwave" in lower and not lower.endswith((".md", ".txt", ".zip", ".tar", ".gz")):
                full = os.path.join(tools_dir, f)
                if os.path.isfile(full):
                    logger.info(f"Found dingwave binary: {f}")
                    return full

    # Return default (will fail later with clear message)
    return os.path.join(tools_dir, candidates[0])


# --- DingTalk data paths ---
# Auto-detection is tried first. If it fails (e.g. DingTalk not installed),
# set environment variables or edit the defaults below.
#
# Environment variables (recommended for override):
#   DINGTALK_UID       = your real user UID (for V3 this differs from the folder name)
#   DINGTALK_DATA_DIR  = full path to your DingTalk data directory
#
# Manual defaults (fallback when auto-detection and env vars both fail):
_detected_dir, _detected_uid = _detect_dingtalk_user()

DINGTALK_DATA_DIR = _detected_dir or r"C:\Users\<YOUR_USERNAME>\AppData\Roaming\DingTalk\<YOUR_UID>_v2"
USER_UID = _detected_uid or "<YOUR_UID>"

ENCRYPTED_DB_DIR = os.path.join(DINGTALK_DATA_DIR, "DBFiles")
ENCRYPTED_DB = os.path.join(ENCRYPTED_DB_DIR, "dingtalk.db")

# dingwave tool — auto-detected
DINGWAVE_PATH = _detect_dingwave()

# Sync settings
SYNC_INTERVAL_HOURS = 4
COPY_RETRY_COUNT = 3
COPY_RETRY_DELAY = 30  # seconds

# Data directories
DECRYPTED_DIR = os.path.join(DATA_DIR, "decrypted")
EXPORT_DIR = os.path.join(DATA_DIR, "exports")
LOGS_DIR = os.path.join(PROJECT_DIR, "logs")

# File paths
DECRYPTED_DB_PATH = os.path.join(DECRYPTED_DIR, "dingtalk.db")
DECRYPTED_ACCOUNT_FILE = os.path.join(DECRYPTED_DIR, "account.json")
SYNC_STATE_FILE = os.path.join(DATA_DIR, "sync_state.json")


def configure_account(uid, data_dir, persist=False):
    """Update the active account used by all runtime modules."""
    global USER_UID, DINGTALK_DATA_DIR, ENCRYPTED_DB_DIR, ENCRYPTED_DB

    selected_uid = str(uid)
    selected_dir = os.path.normpath(data_dir)

    USER_UID = selected_uid
    DINGTALK_DATA_DIR = selected_dir
    ENCRYPTED_DB_DIR = os.path.join(DINGTALK_DATA_DIR, "DBFiles")
    ENCRYPTED_DB = os.path.join(ENCRYPTED_DB_DIR, "dingtalk.db")

    if persist:
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            temp_path = SELECTED_ACCOUNT_FILE + ".tmp"
            with open(temp_path, "w", encoding="utf-8") as stream:
                json.dump(
                    {"uid": selected_uid, "data_dir": selected_dir},
                    stream,
                    ensure_ascii=False,
                    indent=2,
                )
            os.replace(temp_path, SELECTED_ACCOUNT_FILE)
        except OSError as exc:
            logger.warning(f"Could not persist selected DingTalk account: {exc}")


def get_active_account():
    return {"uid": USER_UID, "data_dir": DINGTALK_DATA_DIR}

# Attachment directories (relative to DINGTALK_DATA_DIR)
ATTACHMENT_DIRS = {
    "image": "ImageFiles",
    "audio": "AudioFiles",
    "video": "VideoFiles",
    "resource_cache": "resource_cache",
}

# Message content types
CONTENT_TYPE_TEXT = 1
CONTENT_TYPE_IMAGE = 2
CONTENT_TYPE_VOICE = 300
CONTENT_TYPE_FILE = 501
CONTENT_TYPE_RICH_TEXT = 1200
CONTENT_TYPE_INTERACTIVE_CARD = 2900
CONTENT_TYPE_MINI_APP_CARD = 2950
CONTENT_TYPE_QUOTE = 3100
CONTENT_TYPE_VIDEO_CALL = 1101
CONTENT_TYPE_APPROVAL = 1400

# Content type names for display
CONTENT_TYPE_NAMES = {
    1: "文本",
    2: "图片",
    4: "文件",
    102: "系统消息",
    104: "系统通知",
    202: "链接",
    300: "语音",
    400: "视频",
    500: "位置",
    501: "文件",
    503: "文件",
    1101: "通话",
    1200: "富文本",
    1201: "交互卡片",
    1202: "系统提示",
    1400: "审批",
    1500: "任务",
    1600: "日程",
    2900: "互动卡片",
    2950: "小程序卡片",
    3100: "引用消息",
}

# Web server settings
WEB_HOST = "0.0.0.0"
WEB_PORT = 8090

# Ensure directories exist
for d in [DATA_DIR, DECRYPTED_DIR, EXPORT_DIR, LOGS_DIR]:
    os.makedirs(d, exist_ok=True)
