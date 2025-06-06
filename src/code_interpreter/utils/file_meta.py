from datetime import datetime
import os
import sqlite3
import logging
from pathlib import Path
from typing import Optional
from code_interpreter.config import Config
from code_interpreter.utils.validation import parse_duration

config = Config()
os.makedirs(config.file_storage_path, exist_ok=True)
_DB = Path(config.file_storage_path) / "file_mgmt_db.sqlite3"
_CONN = sqlite3.connect(_DB, check_same_thread=False, isolation_level=None)
_CONN.execute("PRAGMA journal_mode=WAL;")

_CONN.execute("""
    CREATE TABLE IF NOT EXISTS files (
        file_hash      TEXT,
        chat_id        TEXT NOT NULL,
        filename       TEXT NOT NULL,
        remaining      INTEGER,  -- NULL = unlimited
        expires_at     TEXT,     -- ISO formatted date or NULL for no expiry
        PRIMARY KEY (file_hash, chat_id, filename)
    );
""")

logger = logging.getLogger("code_interpreter_service")

def _expiry_timestamp(expires_in: str | None) -> str | None:
    delta = parse_duration(expires_in)
    return (datetime.now() + delta).isoformat() if delta else None

def register(
    file_hash: str,
    chat_id: str,
    filename: str,
    max_downloads: int | None = None,
    expires_in: str | None = None,
) -> None:
    if not file_hash or not chat_id or not filename:
        raise TypeError("hash/chat_id/filename must be non-empty")

    remaining = None if (max_downloads or 0) == 0 else max_downloads
    expires_at = _expiry_timestamp(expires_in)

    try:
        _CONN.execute(
            """
            INSERT INTO files (file_hash, chat_id, filename, remaining, expires_at)
            VALUES (?,?,?,?,?)
            ON CONFLICT(file_hash, chat_id, filename) DO UPDATE
              SET remaining  = excluded.remaining,
                  expires_at = excluded.expires_at;
            """,
            (file_hash, chat_id, filename, remaining, expires_at),
        )
        logger.info(
            "Registered %s/%s (%s) - dl=%s exp=%s",
            chat_id,
            file_hash,
            filename,
            "∞" if remaining is None else remaining,
            expires_at or "never",
        )
    except Exception as exc:
        logger.error("DB register failed: %s", exc, exc_info=True)

def check_and_decrement(file_hash: str, chat_id: str, filename: str) -> None:
    """Check if a file can be downloaded and decrement its download counter"""
    row = _CONN.execute(
        "SELECT remaining, expires_at FROM files WHERE file_hash = ? AND chat_id = ? AND filename = ?;", 
        (file_hash, chat_id, filename)
    ).fetchone()
    
    if row is None:
        raise FileNotFoundError(f"File {filename} ({file_hash}) not found for chat {chat_id}")

    remaining, expires_at = row

    if expires_at is not None:
        try:
            expiry_date = datetime.fromisoformat(expires_at)
            if datetime.now() > expiry_date:
                # Set remaining to 0 on expiry rather than deleting
                _CONN.execute(
                    "UPDATE files SET remaining = 0 WHERE file_hash = ? AND chat_id = ? AND filename = ?;", 
                    (file_hash, chat_id, filename)
                )
                logger.debug(f"File {filename} has expired by date")
                raise PermissionError(f"File {filename} has expired, aborting download")
        except ValueError:
            logger.error(f"Invalid date format in database: {expires_at}")

    # Check for download count expiration
    if remaining is not None and remaining <= 0:
        raise PermissionError(f"Download limit reached for file {filename}")

    # Decrement counter if limited
    if remaining is not None:
        _CONN.execute(
            "UPDATE files SET remaining = remaining - 1 WHERE file_hash = ? AND chat_id = ? AND filename = ?;", 
            (file_hash, chat_id, filename)
        )
        logger.debug(f"Remaining downloads for {filename}: {remaining-1}")

def expire(file_hash: str, chat_id: str, filename: str) -> None:
    """Set a file's remaining downloads to 0 to mark it as expired"""
    cur = _CONN.execute(
        "UPDATE files SET remaining = 0 "
        "WHERE file_hash = ? AND chat_id = ? AND filename = ?;",
        (file_hash, chat_id, filename),
    )
    if cur.rowcount == 0:
        raise FileNotFoundError("No such file registered")
    
    logger.debug(f"Set file as expired: {chat_id}/{file_hash}/{filename}")

def get_file_info(file_hash: str, chat_id: str, filename: str):
    """Get information about a registered file"""
    row = _CONN.execute(
        "SELECT remaining, expires_at FROM files WHERE file_hash = ? AND chat_id = ? AND filename = ?;", 
        (file_hash, chat_id, filename)
    ).fetchone()
    
    if row is None:
        raise FileNotFoundError(f"File {filename} ({file_hash}) not found for chat {chat_id}")
    
    remaining, expires_at = row
    return {
        "file_hash": file_hash,
        "chat_id": chat_id,
        "filename": filename,
        "remaining_downloads": remaining,
        "expires_at": expires_at
    }

def cleanup_expired_files():
    """Find and set files as expired based on downloads or date"""
    try:
        # Mark files as expired based on date
        _CONN.execute(
            """
            UPDATE files 
            SET remaining = 0 
            WHERE expires_at IS NOT NULL AND expires_at < datetime('now') AND remaining != 0
            """
        )
        
        # Get all expired files (remaining = 0)
        expired = _CONN.execute(
            "SELECT file_hash, chat_id, filename FROM files WHERE remaining = 0"
        ).fetchall()
        
        expired_count = len(expired)
        if expired_count > 0:
            logger.info(f"Found {expired_count} expired files marked for deletion")
            
        # TODO: Uncomment this when confirmed of safety
        '''
        for file_hash, chat_id, filename in expired:
            file_path = os.path.join(config.file_storage_path, chat_id, file_hash, filename)
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
                    # Clean up empty directories
                    hash_dir = os.path.dirname(file_path)
                    if not os.listdir(hash_dir):
                        os.rmdir(hash_dir)
                        chat_dir = os.path.dirname(hash_dir)
                        if not os.listdir(chat_dir):
                            os.rmdir(chat_dir)
                    logger.info(f"Deleted expired file: {chat_id}/{file_hash}/{filename}")
            except Exception as e:
                logger.error(f"Error deleting expired file {file_path}: {str(e)}")
        '''
    except Exception as e:
        logger.error(f"Error in cleanup task: {str(e)}")