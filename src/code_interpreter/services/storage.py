from contextlib import asynccontextmanager
import secrets
import os
from typing import AsyncIterator, Protocol, Optional
from anyio import Path
from pydantic import validate_call
from code_interpreter.utils.file_meta import check_and_decrement

class ObjectReader(Protocol):
    async def read(self, size: int = -1) -> bytes: ...

class ObjectWriter(Protocol):
    hash: str
    filename: str
    chat_id: str

    async def write(self, data: bytes) -> None: ...

class Storage:
    """
    Storage is a collection of objects organized by chat ID and hash.
    Objects consist of binary data and are identified by their SHA-256 hash.
    
    Files are stored in: /storage/CHAT_ID/HASH/FILENAME
    """

    def __init__(self, storage_path: str):
        self.storage_path = Path(storage_path)

    @asynccontextmanager
    async def writer(self, filename: str, chat_id: str):
        hash_ = secrets.token_hex(32)

        chat_dir = self.storage_path / chat_id
        hash_dir = chat_dir / hash_
        await chat_dir.mkdir(parents=True, exist_ok=True)
        await hash_dir.mkdir(exist_ok=True)

        file_path = hash_dir / filename

        try:
            async with await file_path.open("wb") as f:
                # expose a few attrs to callers for convenience
                f.hash = hash_
                f.filename = filename
                f.chat_id = chat_id
                yield f
        except Exception:
            if await file_path.exists():
                await file_path.unlink()
            await hash_dir.rmdir()
            raise      

    async def write(self, data: bytes, filename: str, chat_id: str) -> str:
        """
        Writes the data to the storage and returns the hash of the object.
        """
        async with self.writer(filename=filename, chat_id=chat_id) as f:
            await f.write(data)
            return f.hash

    @asynccontextmanager
    @validate_call
    async def reader(self, object_hash: str, chat_id: str, filename: str) -> AsyncIterator[ObjectReader]:
        """
        Async context manager that opens an object for reading.
        """
        try:
            check_and_decrement(
                file_hash=object_hash, 
                chat_id=chat_id,
                filename=filename
            )
        except Exception as e:
            raise e
        
        target_dir = self.storage_path / chat_id / object_hash
        target_file = target_dir / filename
        
        if not object_hash or not await target_file.exists():
            raise FileNotFoundError(f"File not found: {target_file}")
            
        async with await target_file.open("rb") as f:
            yield f

    @validate_call
    async def read(self, object_hash: str, chat_id: str, filename: str) -> bytes:
        """
        Reads the object with the given hash and returns it.
        """
        async with self.reader(object_hash=object_hash, chat_id=chat_id, filename=filename) as f:
            return await f.read()

    @validate_call
    async def exists(self, object_hash: str, chat_id: str, filename: str) -> bool:
        """
        Check if an object with the given hash exists in the storage.
        """
        return await (self.storage_path / chat_id / object_hash / filename).exists()