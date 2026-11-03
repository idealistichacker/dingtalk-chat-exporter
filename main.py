import logging
import os
import sys

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from decrypt import ensure_decrypted_database
from web.api import app

import uvicorn

# Configure logging
log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
logging.basicConfig(
    level=logging.INFO,
    format=log_format,
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            os.path.join(config.LOGS_DIR, "dingtalk-exporter.log"),
            encoding="utf-8",
        ),
    ],
)

logger = logging.getLogger(__name__)


if __name__ == "__main__":
    logger.info(f"Starting DingTalk Exporter on {config.WEB_HOST}:{config.WEB_PORT}")
    logger.info(f"Data directory: {config.DINGTALK_DATA_DIR}")
    logger.info(f"dingwave path: {config.DINGWAVE_PATH}")
    logger.info(f"Sync interval: every {config.SYNC_INTERVAL_HOURS} hours")

    # Startup validation: warn early about missing components
    if (
        not config.DINGTALK_DATA_DIR.lower().endswith("_v3")
        and not os.path.isfile(config.DINGWAVE_PATH)
    ):
        logger.warning(
            f"dingwave binary not found at: {config.DINGWAVE_PATH}\n"
            f"Please download from https://github.com/p1g3/dingwave/releases\n"
            f"Decryption and sync will fail until this is resolved."
        )
    if not os.path.exists(config.ENCRYPTED_DB):
        logger.warning(
            f"DingTalk database not found at: {config.ENCRYPTED_DB}\n"
            f"Make sure DingTalk desktop client is installed and logged in on this machine."
        )

    try:
        ensure_decrypted_database()
    except Exception as exc:
        logger.error(f"Initial database decryption failed: {exc}")
        logger.error(
            "The web server was not started. Check the selected DingTalk UID "
            "and make sure a supported *_v2 or *_v3 database is available."
        )
        raise SystemExit(1)

    uvicorn.run(
        app,
        host=config.WEB_HOST,
        port=config.WEB_PORT,
        log_level="info",
    )
