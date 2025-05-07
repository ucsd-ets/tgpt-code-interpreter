import os
import sqlite3
import logging
from pathlib import Path
from typing import Optional
from code_interpreter.config import Config

config = Config()
os.makedirs(config.file_storage_path, exist_ok=True)
_DB = Path(config.file_storage_path) / "file_mgmt_db.sqlite3"
_CONN = sqlite3.connect(_DB, check_same_thread=False, isolation_level=None)
_CONN.execute("PRAGMA journal_mode=WAL;")

# Update schema to include filename
_CONN.execute("""
    CREATE TABLE IF NOT EXISTS files (
        file_hash      TEXT,
        chat_id        TEXT NOT NULL,
        filename       TEXT NOT NULL,
        remaining      INTEGER,  -- NULL = unlimited
        PRIMARY KEY (file_hash, chat_id, filename)
    );
""")

logger = logging.getLogger("code_interpreter_service")

def register(file_hash: str, chat_id: str, filename: str, max_downloads: int) -> None:
    """Register a file in the database with download limits"""
    if not file_hash or not chat_id or not filename:
        logger.warning(f"Invalid parameters for file registration: hash={file_hash}, chat_id={chat_id}, filename={filename}")
        return
        
    remaining = None if max_downloads == 0 else max_downloads
    
    try:
        _CONN.execute(
            """
            INSERT INTO files(file_hash, chat_id, filename, remaining)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(file_hash, chat_id, filename) DO UPDATE
              SET remaining = excluded.remaining;
            """,
            (file_hash, chat_id, filename, remaining),
        )
        logger.debug(f"Registered file {filename} ({file_hash}) for chat {chat_id} with {remaining if remaining is not None else 'unlimited'} downloads")
    except Exception as e:
        logger.error(f"Error registering file: {str(e)}")

def check_and_decrement(file_hash: str, chat_id: str, filename: str) -> None:
    """Check if a file can be downloaded and decrement its download counter"""
    row = _CONN.execute(
        "SELECT remaining FROM files WHERE file_hash = ? AND chat_id = ? AND filename = ?;", 
        (file_hash, chat_id, filename)
    ).fetchone()
    
    if row is None:
        raise FileNotFoundError(f"File {filename} ({file_hash}) not found for chat {chat_id}")

    remaining = row[0]
    if remaining is not None and remaining <= 0:
        raise PermissionError(f"Download limit reached for file {filename}")

    # decrement counter if limited
    if remaining is not None:
        _CONN.execute(
            "UPDATE files SET remaining = remaining - 1 WHERE file_hash = ? AND chat_id = ? AND filename = ?;", 
            (file_hash, chat_id, filename)
        )
        logger.debug(f"Remaining downloads for {filename}: {remaining-1}")
        if remaining - 1 <= 0:
            logger.debug(f"Removing file record for {filename} ({file_hash})")
            _CONN.execute(
                "DELETE FROM files WHERE file_hash = ? AND chat_id = ? AND filename = ?;", 
                (file_hash, chat_id, filename)
            )

def cleanup_expired_files():
    """Remove records with remaining=0 and clean up orphaned files"""
    try:
        # Get all expired file records
        expired = _CONN.execute(
            "SELECT file_hash, chat_id, filename FROM files WHERE remaining = 0"
        ).fetchall()
        
        # Delete records from database
        if expired:
            _CONN.execute("DELETE FROM files WHERE remaining = 0")
            
            # Delete files from disk
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
    except Exception as e:
        logger.error(f"Error in cleanup task: {str(e)}")