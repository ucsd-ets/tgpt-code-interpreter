import json
import logging
from pathlib import Path
import re
from typing import Optional
from code_interpreter.config import Config

import os
import sqlite3

config = Config()
os.makedirs(config.file_storage_path, exist_ok=True)
_DB = Path(config.file_storage_path) / "file_mgmt_db.sqlite3"
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
    if not file_hash or not re.match(r'^[0-9a-zA-Z_-]{1,255}$', file_hash):
        logger.warning(f"Invalid file hash format: {file_hash}")
        return
        
    if config.require_chat_id and not chat_id:
        logger.warning("Attempted to register file without chat_id when required")
        return
    
    remaining = None if max_downloads == 0 else max_downloads
    
    try:
        _CONN.execute(
            """
            INSERT INTO files(file_hash, chat_id, remaining)
            VALUES (?, ?, ?)
            ON CONFLICT(file_hash) DO UPDATE
              SET chat_id = excluded.chat_id,
                  remaining = excluded.remaining;
            """,
            (file_hash, chat_id, remaining),
        )
        logger.debug(f"Registered file {file_hash} for chat {chat_id} with {remaining if remaining is not None else 'unlimited'} downloads")
    except Exception as e:
        logger.error(f"Error registering file: {str(e)}")

def check_and_decrement(file_hash: str, chat_id: Optional[str]) -> None:
    row = _CONN.execute(
        "SELECT chat_id, remaining FROM files WHERE file_hash = ?;", (file_hash,)
    ).fetchone()
    if row is None:
        raise FileNotFoundError

    db_chat, remaining = row
    if config.require_chat_id and db_chat != chat_id:
        raise PermissionError("chat-id")

    if remaining is not None and remaining <= 0:
        raise PermissionError("expired")

    # decrement counter if limited
    # TODO:  and config.global_max_downloads != 0?
    if remaining is not None:
        _CONN.execute(
            "UPDATE files SET remaining = remaining - 1 WHERE file_hash = ?;", (file_hash,)
        )
        logger.debug(f"Remaining: {str(remaining)}")
        if remaining - 1 <= 0:
            logger.debug(f"Removing file {file_hash}...")
            _CONN.execute("DELETE FROM files WHERE file_hash = ?;", (file_hash,))

def cleanup_expired_files():
    """Remove records with remaining=0 and clean up orphaned files"""
    try:
        # Get all expired file records
        expired = _CONN.execute("SELECT file_hash FROM files WHERE remaining = 0").fetchall()
        
        # Delete records from database
        if expired:
            _CONN.execute("DELETE FROM files WHERE remaining = 0")
            
            # Delete files from disk
            for (file_hash,) in expired:
                file_path = os.path.join(config.file_storage_path, file_hash)
                try:
                    if os.path.exists(file_path):
                        os.remove(file_path)
                        logger.info(f"Deleted expired file: {file_hash}")
                except Exception as e:
                    logger.error(f"Error deleting expired file {file_hash}: {str(e)}")
    except Exception as e:
        logger.error(f"Error in cleanup task: {str(e)}")