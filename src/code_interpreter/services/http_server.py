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

import re
import json
import logging
import uuid
from contextvars import ContextVar
from typing import List, Dict, Union

from code_interpreter.utils.validation import AbsolutePath, Hash
from fastapi import FastAPI, HTTPException, Depends, status, Body
from json_repair import repair_json
import fastjsonschema
import pathlib
import os
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from code_interpreter.services.custom_tool_executor import (
    CustomToolExecuteError,
    CustomToolExecutor,
    CustomToolParseError,
)
from code_interpreter.services.kubernetes_code_executor import KubernetesCodeExecutor

logger = logging.getLogger("code_interpreter_service")

SCHEMA_PATH = os.getenv("BEE_SCHEMA_PATH")
_validate = (
    fastjsonschema.compile(
        json.loads(pathlib.Path(SCHEMA_PATH).read_text())
    )
    if SCHEMA_PATH
    else None
)

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
    async def execute(
        raw_input: Union[dict, str] = Body(
            ...,
            description="Either a JSON object (application/json) matching ExecuteRequest, or raw text (text/plain) containing malformed JSON or Python code"
        ),
        request_id: str = Depends(set_request_id),
    ):
        """
        1. FastAPI will deserialize JSON bodies into dicts, or plain-text into str.
        2. If dict → assume valid JSON envelope and skip repair.
        3. If str → attempt repair_json + json.loads, or error 422.
        4. Unwrap {"requestBody": {...}} if present.
        5. Canonicalise keys (aliases + camel→snake).
        6. Validate against BEE_SCHEMA_PATH if configured.
        7. Cast to ExecuteRequest and invoke the sandbox executor.
        """
        logger.info("Sanitizing incoming request")

        # 2 & 3. Normalize to payload dict
        if isinstance(raw_input, dict):
            payload = raw_input
        else:
            # raw_input is str → repair & parse
            try:
                fixed = repair_json(raw_input)
                logger.debug("json-repair applied")
                payload = json.loads(fixed)
            except Exception as e:
                logger.error("json-repair / parsing failed", exc_info=True)
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Invalid JSON and repair failed: {e}"
                ) from e

        # 4. Drop wrapper if present
        if isinstance(payload, dict) and set(payload) == {"requestBody"}:
            payload = payload["requestBody"]

        # 5. Canonicalise keys
        payload = canonicalise(payload)

        # 6. Optional JSON Schema validation
        if _validate:
            try:
                _validate(payload)
            except Exception as e:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Schema validation failed: {e}"
                ) from e

        # 7. Pydantic model and execution
        request = ExecuteRequest.model_validate(payload)
        logger.info(
            "Executing code with files %s: %s", request.files, request.source_code
        )
        try:
            result = await code_executor.execute(
                source_code=request.source_code,
                files=request.files,
                env=request.env,
            )
        except Exception as e:
            logger.exception("Error executing code")
            raise HTTPException(status_code=500, detail=str(e))

        logger.info("Code execution completed with result %s", result)
        return result

    @app.post(
        "/v1/parse-custom-tool",
        response_model=ParseCustomToolResponse,
    )
    async def parse_custom_tool(
        request: ParseCustomToolRequest,
        request_id: str = Depends(set_request_id)
    ):
        logger.info("Parsing custom tool with source code")
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

    @app.post(
        "/v1/execute-custom-tool",
        response_model=ExecuteCustomToolResponse,
    )
    async def execute_custom_tool(
        request: ExecuteCustomToolRequest,
        request_id: str = Depends(set_request_id),
    ):
        logger.info("Executing custom tool with source code")
        result = await custom_tool_executor.execute(
            tool_input_json=request.tool_input_json,
            tool_source_code=request.tool_source_code,
            env=request.env,
        )
        return ExecuteCustomToolResponse(tool_output_json=json.dumps(result))

    @app.exception_handler(CustomToolExecuteError)
    async def handle_execute_error(request, e):
        logger.warning("Error executing custom tool: %s", e)
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=ExecuteCustomToolErrorResponse(stderr=str(e)).model_dump(),
        )

    return app
