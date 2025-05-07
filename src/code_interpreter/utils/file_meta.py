import json
import logging
from pathlib import Path
from typing import Optional
from config import Config

import sqlite3

_DB = Path(Config.file_storage_path) / "file_mgmt_db.sqlite3"
_CONN = sqlite3.connect(_DB, check_same_thread=False, isolation_level=None)
_CONN.execute("PRAGMA journal_mode=WAL;")
_CONN.execute(
    """
    CREATE TABLE IF NOT EXISTS files (
        file_hash      TEXT PRIMARY KEY,
        chat_id   TEXT,
        remaining INTEGER  -- NULL = unlimited
    );
    """
)
logger = logging.getLogger("code_interpreter_service")


def register(file_hash: str, chat_id: Optional[str], max_downloads: int) -> None:
    remaining = None if max_downloads == 0 else max_downloads
    _CONN.execute(
        """
        INSERT INTO files(file_hash, chat_id, remaining)
        VALUES (?, ?, ?)
        ON CONFLICT(file_hash) DO UPDATE
          SET chat_id   = excluded.chat_id,
              remaining = excluded.remaining;
        """,
        (file_hash, chat_id, remaining),
    )

def check_and_decrement(file_hash: str, chat_id: Optional[str]) -> None:
    row = _CONN.execute(
        "SELECT chat_id, remaining FROM files WHERE file_hash = ?;", (file_hash,)
    ).fetchone()
    if row is None:
        raise FileNotFoundError

    db_chat, remaining = row
    if Config.require_chat_id and db_chat != chat_id:
        raise PermissionError("chat-id")

    if remaining is not None and remaining <= 0:
        raise PermissionError("expired")

    # decrement counter if limited
    # TODO:  and Config.global_max_downloads != 0?
    if remaining is not None:
        _CONN.execute(
            "UPDATE files SET remaining = remaining - 1 WHERE file_hash = ?;", (file_hash,)
        )
        logger.debug(f"Remaining: {str(remaining)}")
        if remaining - 1 <= 0:
            logger.debug(f"Removing file {file_hash}...")
            _CONN.execute("DELETE FROM files WHERE hash = ?;", (file_hash,))