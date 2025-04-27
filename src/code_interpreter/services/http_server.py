import re
import json
import logging
import uuid
from contextvars import ContextVar
from typing import List, Dict, Union
import pathlib
import os

import fastjsonschema
from json_repair import repair_json

from fastapi import FastAPI, HTTPException, Depends, status, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from code_interpreter.utils.validation import AbsolutePath, Hash
from code_interpreter.services.custom_tool_executor import (
    CustomToolExecuteError,
    CustomToolExecutor,
    CustomToolParseError,
)
from code_interpreter.services.kubernetes_code_executor import KubernetesCodeExecutor

logger = logging.getLogger("code_interpreter_service")

# Optional JSON‐schema validation, if BEE_SCHEMA_PATH is set
SCHEMA_PATH = os.getenv("BEE_SCHEMA_PATH")
_validate = (
    fastjsonschema.compile(json.loads(pathlib.Path(SCHEMA_PATH).read_text()))
    if SCHEMA_PATH
    else None
)

# Aliases and camel→snake conversion
ALIASES = {
    "sourceCode": "source_code",
    "code": "source_code",
    "timeoutSeconds": "timeout",
}
_CAMEL_RE = re.compile(r"(?<!^)(?=[A-Z])")
def camel_to_snake(name: str) -> str:
    return _CAMEL_RE.sub("_", name).lower()

def canonicalise(obj):
    """Recursively normalize dict keys to snake_case and apply ALIASES."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            k2 = ALIASES.get(k, camel_to_snake(k))
            out[k2] = canonicalise(v)
        return out
    if isinstance(obj, list):
        return [canonicalise(i) for i in obj]
    return obj

# Pydantic models
class ExecuteRequest(BaseModel):
    source_code: str
    files: Dict[AbsolutePath, Hash] = {}
    env: Dict[str, str] = {}

class ExecuteResponse(BaseModel):
    stdout: str
    stderr: str
    exit_code: int
    files: Dict[AbsolutePath, Hash]

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

def create_http_server(
    code_executor: KubernetesCodeExecutor,
    custom_tool_executor: CustomToolExecutor,
    request_id_context_var: ContextVar[str],
):
    app = FastAPI()

    def set_request_id():
        request_id = str(uuid.uuid4())
        request_id_context_var.set(request_id)
        return request_id

    @app.post("/v1/execute", response_model=ExecuteResponse)
    async def execute(request: Request, request_id: str = Depends(set_request_id)):
        logger.info("Sanitizing incoming request")
        # 1. Read raw body
        raw = await request.body()
        raw_text = raw.decode("utf-8", errors="replace")

        # 2. Try json.loads
        try:
            payload = json.loads(raw_text)
        except Exception:
            # 3. Try repair_json → json.loads
            try:
                fixed = repair_json(raw_text)
                logger.debug("json-repair applied")
                payload = json.loads(fixed)
            except Exception as e:
                # 4. Fallback: treat entire body as source code
                logger.debug("json-repair failed (%s), falling back to raw source_code", e)
                payload = {"source_code": raw_text, "files": {}, "env": {}}

        # 5. Unwrap {"requestBody": {...}} if present
        if isinstance(payload, dict) and set(payload.keys()) == {"requestBody"}:
            payload = payload["requestBody"]

        # 6. Canonicalise
        payload = canonicalise(payload)

        # 7. JSON‐schema validation
        if _validate:
            try:
                _validate(payload)
            except Exception as e:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Schema validation failed: {e}"
                ) from e

        # 8. Pydantic coercion
        try:
            req = ExecuteRequest.model_validate(payload)
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid request format: {e}"
            ) from e

        logger.info("Executing code with files %s", req.files)
        try:
            result = await code_executor.execute(
                source_code=req.source_code,
                files=req.files,
                env=req.env,
            )
        except Exception as e:
            logger.exception("Error executing code")
            raise HTTPException(status_code=500, detail=str(e))

        logger.info("Code execution completed")
        return result

    @app.post("/v1/parse-custom-tool", response_model=ParseCustomToolResponse)
    async def parse_custom_tool(
        request: ParseCustomToolRequest,
        request_id: str = Depends(set_request_id),
    ):
        logger.info("Parsing custom tool source")
        custom_tool = custom_tool_executor.parse(
            tool_source_code=request.tool_source_code
        )
        return ParseCustomToolResponse(
            tool_name=custom_tool.name,
            tool_input_schema_json=json.dumps(custom_tool.input_schema),
            tool_description=custom_tool.description,
        )

    @app.exception_handler(CustomToolParseError)
    async def handle_parse_error(request, e):
        logger.warning("Invalid custom tool: %s", e.errors)
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=ParseCustomToolErrorResponse(error_messages=e.errors).model_dump(),
        )

    @app.post("/v1/execute-custom-tool", response_model=ExecuteCustomToolResponse)
    async def execute_custom_tool(
        request: ExecuteCustomToolRequest,
        request_id: str = Depends(set_request_id),
    ):
        logger.info("Executing custom tool")
        output = await custom_tool_executor.execute(
            tool_input_json=request.tool_input_json,
            tool_source_code=request.tool_source_code,
            env=request.env,
        )
        return ExecuteCustomToolResponse(tool_output_json=json.dumps(output))

    @app.exception_handler(CustomToolExecuteError)
    async def handle_execute_error(request, e):
        logger.warning("Custom tool execution error: %s", e)
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=ExecuteCustomToolErrorResponse(stderr=str(e)).model_dump(),
        )

    return app
