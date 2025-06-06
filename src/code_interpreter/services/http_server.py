# Copyright 2024 IBM Corp.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import asyncio
import base64
from collections import defaultdict
from ipaddress import ip_network, ip_address
import re
import json
import logging
import time
import uuid
from contextvars import ContextVar
from typing import List, Dict

from code_interpreter.config import Config
from code_interpreter.utils.validation import AbsolutePath, Hash
from fastapi import FastAPI, HTTPException, Depends, status, Request, BackgroundTasks, UploadFile, File, Form
from json_repair import repair_json
import fastjsonschema, pathlib, os, json
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from starlette.status import HTTP_403_FORBIDDEN, HTTP_410_GONE
import mimetypes, re

from code_interpreter.services.custom_tool_executor import (
    CustomToolExecuteError,
    CustomToolExecutor,
    CustomToolParseError,
)
from code_interpreter.services.kubernetes_code_executor import KubernetesCodeExecutor

from code_interpreter.utils.file_meta import check_and_decrement, cleanup_expired_files, register, get_file_info

from kubernetes.utils.quantity import parse_quantity
from code_interpreter.utils.validation import parse_duration

logger = logging.getLogger("code_interpreter_service")

config = Config()
SCHEMA_PATH = os.getenv("BEE_SCHEMA_PATH")
_validate = fastjsonschema.compile(
    json.loads(pathlib.Path(SCHEMA_PATH).read_text())
) if SCHEMA_PATH else None

ALIASES = {
    "sourceCode": "source_code",
    "code":       "source_code",
    "timeoutSeconds": "timeout",
    "limitDownloads": "limit",
}
_CAMEL_RE = re.compile(r"(?<!^)(?=[A-Z])")

def camel_to_snake(name: str) -> str:
    return _CAMEL_RE.sub("_", name).lower()

def canonicalise(obj):
    """Recursively normalise dict keys to snake_case and apply ALIASES."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            k2 = ALIASES.get(k, camel_to_snake(k))
            out[k2] = canonicalise(v)
        return out
    if isinstance(obj, list):
        return [canonicalise(i) for i in obj]
    return obj

class FileMetadata(BaseModel):
    remaining_downloads: int | None = None
    expires_at: str | None = None

class ExecuteRequest(BaseModel):
    source_code: str
    files: Dict[AbsolutePath, Hash] = {}
    env: Dict[str, str] = {}
    chat_id: str = "default"

    max_downloads: int | None = None
    expires_in: str | None = None

    persistent_workspace: bool = False


class ExecuteResponse(BaseModel):
    stdout: str
    stderr: str
    exit_code: int
    files: Dict[AbsolutePath, Hash]
    files_metadata: Dict[AbsolutePath, FileMetadata] = {}
    chat_id: str | None = None

class ParseCustomToolRequest(BaseModel):
    tool_source_code: str


class ParseCustomToolResponse(BaseModel):
    tool_name: str
    tool_input_schema_json: str
    tool_description: str


class ParseCustomToolErrorResponse(BaseModel):
    error_messages: List[str]


class ExecuteCustomToolRequest(BaseModel):
    tool_source_code: str
    tool_input_json: str
    env: Dict[str, str] = {}


class ExecuteCustomToolResponse(BaseModel):
    tool_output_json: str


class ExecuteCustomToolErrorResponse(BaseModel):
    stderr: str

class FileRequest(BaseModel):
    chat_id: str
    file_hash: str
    filename: str

class FileResponse(BaseModel):
    filename: str
    content_type: str
    content_base64: str

class UploadResponse(BaseModel):
    file_hash: str
    filename: str
    chat_id: str
    metadata: FileMetadata

class ExpireRequest(BaseModel):
    chat_id: str
    file_hash: str
    filename: str

class ExpireResponse(BaseModel):
    success: bool

def _is_internal_request(req: Request) -> bool:
    host_ok = req.headers.get("host", "") in config.internal_ip_allowlist
    ip_ok = any(
        ip_address(req.client.host) in ip_network(cidr) or "127.0.0.1"
        for cidr in config.internal_ip_allowlist
    )
    return host_ok or ip_ok


def _guard_spawn(req: Request):
    if config.public_spawn_enabled:
        return
    if not _is_internal_request(req):
        raise HTTPException(
            HTTP_403_FORBIDDEN,
            detail="Spawn requests must originate from an internal URL / IP",
        )

def create_http_server(
    code_executor: KubernetesCodeExecutor,
    custom_tool_executor: CustomToolExecutor,
    request_id_context_var: ContextVar[str],
):
    # vars
    app = FastAPI()

    async def periodic_cleanup():
        while True:
            try:
                logger.info("Running scheduled file cleanup task")
                cleanup_expired_files()
                logger.info("Scheduled file cleanup completed")
            except Exception as e:
                logger.error(f"Error in periodic cleanup: {str(e)}")
            
            await asyncio.sleep(3 * 60 * 60) 
        
    asyncio.create_task(periodic_cleanup())
    logger.info("Scheduled file cleanup task to run every 3 hours")
    
    def set_request_id():
        request_id = str(uuid.uuid4())
        request_id_context_var.set(request_id)
        return request_id

    @app.post("/v1/upload", response_model=UploadResponse)
    async def upload_file(
        raw_request: Request,
        chat_id: str = Form(..., pattern=r"^[0-9a-zA-Z_-]{1,255}$"),
        upload: UploadFile = File(...),
        max_downloads: int = Form(None),
        expires_in: str | None = Form(None),
        request_id: str = Depends(set_request_id),
    ):
        _guard_spawn(raw_request)

        if not re.match(r"^[0-9A-Za-z._-]{1,255}$", upload.filename):
            raise HTTPException(400, "Invalid filename")

        # convert K8s quantity ("1Gi") → int bytes using k8s-client helper
        try:
            max_bytes = int(parse_quantity(config.file_size_limit))
        except Exception as e:
            logger.error("Bad file_size_limit %s - %s",
                         config.file_size_limit, e, exc_info=True)
            max_bytes = 1_073_741_824

        try:  
            bytes_seen = 0
            async with code_executor.file_storage.writer(
                filename=upload.filename, chat_id=chat_id
            ) as dest:
                while chunk := await upload.read(8192):
                    bytes_seen += len(chunk)
                    if bytes_seen > max_bytes:
                        raise HTTPException(413, "File too large")
                    await dest.write(chunk)

            register(
                file_hash=dest.hash,
                filename=upload.filename,
                chat_id=chat_id,
                max_downloads=max_downloads,
                expires_in=expires_in,
            )

            meta = get_file_info(dest.hash, chat_id, upload.filename)

            logger.info(
                "Uploaded %s (%d bytes) to chat %s (dl=%s, exp=%s)",
                upload.filename,
                bytes_seen,
                chat_id,
                meta['remaining_downloads'] or "∞",
                meta['expires_at'] or "never",
            )

            return UploadResponse(
                file_hash=dest.hash,
                filename=upload.filename,
                chat_id=chat_id,
                metadata=FileMetadata(
                    remaining_downloads=meta["remaining_downloads"],
                    expires_at=meta["expires_at"],
                ),
            )

        except HTTPException:
            raise
        except Exception as exc:
            logger.error("Upload failed: %s", exc, exc_info=True)
            raise HTTPException(500, "Upload failed.")


    @app.post("/v1/expire", response_model=ExpireResponse)
    async def expire_file(
        raw_request: Request,
        request: ExpireRequest,
        request_id: str = Depends(set_request_id),
    ):
        try:
            from code_interpreter.utils.file_meta import expire
            expire(
                file_hash=request.file_hash,
                chat_id=request.chat_id,
                filename=request.filename,
            )
            logger.info("Expired %s/%s/%s",
                        request.chat_id, request.file_hash, request.filename)
            return ExpireResponse(success=True)

        except FileNotFoundError:
            # keep behaviour symmetric with download
            raise HTTPException(404, "File not found")

        except Exception as e:
            logger.error("Expire failed: %s", e, exc_info=True)
            raise HTTPException(500, f"Expire failed: {e}")

    @app.post("/v1/download", response_model=FileResponse)
    async def download(
        raw_request: Request,
        request: FileRequest,
        background_tasks: BackgroundTasks,
        request_id: str = Depends(set_request_id),
    ):
        # Validate input parameters to prevent path traversal
        if not request.file_hash or not re.match(r'^[0-9a-zA-Z_-]{1,255}$', request.file_hash):
            raise HTTPException(400, "Invalid file hash format")
        
        if not request.chat_id or not re.match(r'^[0-9a-zA-Z_-]{1,255}$', request.chat_id):
            raise HTTPException(400, "Invalid chat ID format")
        
        if not request.filename or not re.match(r'^[0-9a-zA-Z._-]{1,255}$', request.filename):
            raise HTTPException(400, "Invalid filename format")
        
        # Check download permissions
        try:
            check_and_decrement(
                file_hash=request.file_hash, 
                chat_id=request.chat_id,
                filename=request.filename
            )
        # Use 404 for security (no information disclosure)
        except FileNotFoundError:
            logger.warning(f"File not found: {request.chat_id}/{request.file_hash}/{request.filename}")
            raise HTTPException(404, f"File not found")
        except PermissionError:
            logger.warning(f"Download limit reached: {request.chat_id}/{request.file_hash}/{request.filename}")
            raise HTTPException(404, f"File not found")
        except Exception as e:
            logger.error(f"Error checking file permissions: {str(e)}")
            raise HTTPException(404, f"File not found")

        # Build correct file path with new structure
        filepath = os.path.join(
            config.file_storage_path, 
            request.chat_id,
            request.file_hash, 
            request.filename
        )
        
        # Check if file exists
        if not os.path.exists(filepath):
            logger.warning(f"File found in DB but not on disk: {request.chat_id}/{request.file_hash}/{request.filename}")
            raise HTTPException(404, f"File not found on disk")
        
        try:
            # Detect the file type
            content_type = mimetypes.guess_type(request.filename)[0] or "application/octet-stream"
            file_size = os.path.getsize(filepath)
            
            # Stream file in chunks
            def file_streamer():
                with open(filepath, "rb") as file:
                    while chunk := file.read(8192):
                        yield chunk
            
            logger.info(f"Serving file {request.filename} ({request.file_hash}) to chat {request.chat_id}")
            
            # Return streaming response
            return StreamingResponse(
                file_streamer(),
                media_type=content_type,
                headers={
                    "Content-Disposition": f"attachment; filename={request.filename}",
                    "Content-Length": str(file_size),
                }
            )
        except Exception as e:
            logger.error(f"Error reading file: {str(e)}")
            raise HTTPException(500, f"Error reading file: {str(e)}")


    @app.post("/v1/execute", response_model=ExecuteResponse)
    async def execute(
        raw_request: Request,
        request_id: str = Depends(set_request_id),
    ):
        _guard_spawn(raw_request)

        payload = json.loads(await raw_request.body())
        if isinstance(payload, dict) and set(payload) == {"requestBody"}:
            payload = payload["requestBody"]

        payload = canonicalise(payload)
        if _validate:
            _validate(payload)

        request = ExecuteRequest.model_validate(payload)

        if config.require_chat_id and not request.chat_id:
            raise HTTPException(403, "Chat ID required but missing")

        try:
            result = await code_executor.execute(
                source_code=request.source_code,
                files=request.files,
                env=request.env,
                chat_id=request.chat_id,
                persistent_workspace=request.persistent_workspace,
            )

            files_metadata: Dict[AbsolutePath, FileMetadata] = {}

            if request.persistent_workspace and result.files:
                for abs_path, file_hash in result.files.items():
                    filename = os.path.basename(abs_path)

                    register(
                        file_hash=file_hash,
                        chat_id=request.chat_id,
                        filename=filename,
                        max_downloads=request.max_downloads,
                        expires_in=request.expires_in,
                    )

                    info = get_file_info(file_hash, request.chat_id, filename)
                    files_metadata[abs_path] = FileMetadata(
                        remaining_downloads=info["remaining_downloads"],
                        expires_at=info["expires_at"],
                    )

            return ExecuteResponse(
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.exit_code,
                files=result.files,
                files_metadata=files_metadata,
                chat_id=result.chat_id,
            )
        
            logger.info("Code execution completed with result %s", result)
            return response

        except Exception as e:
            logger.exception("Execution failure: %s", e)
            raise HTTPException(500, str(e))

    @app.post(
        "/v1/parse-custom-tool",
        response_model=ParseCustomToolResponse,
    )
    async def parse_custom_tool(
        raw_request: Request, request: ParseCustomToolRequest, request_id: str = Depends(set_request_id)
    ):
        _guard_spawn(raw_request)

        raise HTTPException(401, "Method disabled")

        logger.info("Parsing custom tool with source code %s", request.tool_source_code)
        custom_tool = custom_tool_executor.parse(
            tool_source_code=request.tool_source_code
        )
        result = ParseCustomToolResponse(
            tool_name=custom_tool.name,
            tool_input_schema_json=json.dumps(custom_tool.input_schema),
            tool_description=custom_tool.description,
        )
        logger.info("Parsed custom tool %s", result)
        return result

    @app.exception_handler(CustomToolParseError)
    async def validation_exception_handler(request, e):
        logger.warning("Invalid custom tool: %s", e.errors)
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=ParseCustomToolErrorResponse(error_messages=e.errors).model_dump(),
        )

    @app.post(
        "/v1/execute-custom-tool",
        response_model=ExecuteCustomToolResponse,
    )
    async def execute_custom_tool(
        raw_request: Request,
        request: ExecuteCustomToolRequest,
        request_id: str = Depends(set_request_id),
    ):
        _guard_spawn(raw_request)

        raise HTTPException(401, "Method disabled")

        logger.info(
            "Executing custom tool with source code %s", request.tool_source_code
        )
        result = await custom_tool_executor.execute(
            tool_input_json=request.tool_input_json,
            tool_source_code=request.tool_source_code,
            env=request.env,
        )
        logger.info("Executed custom tool with result %s", result)
        return ExecuteCustomToolResponse(tool_output_json=json.dumps(result))

    @app.exception_handler(CustomToolExecuteError)
    async def validation_exception_handler(request, e):
        logger.warning("Error executing custom tool: %s", e)
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=ExecuteCustomToolErrorResponse(stderr=str(e)).model_dump(),
        )

    return app
