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
import collections
import logging
import os
import random
import string
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncGenerator, Mapping

import httpx
from pydantic import validate_call
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from code_interpreter.config import Config
from code_interpreter.services.kubectl import Kubectl
from code_interpreter.services.storage import Storage
from code_interpreter.utils.validation import AbsolutePath, Hash

logger = logging.getLogger("kubernetes_code_executor")

config = Config()


class KubernetesCodeExecutor:
    @dataclass
    class Result:
        stdout: str
        stderr: str
        exit_code: int
        files: Mapping[AbsolutePath, Hash]
        chat_id: str = "default"

    def __init__(
        self,
        kubectl: Kubectl,
        executor_image: str,
        container_resources: dict,
        file_storage: Storage,
        executor_pod_spec_extra: dict,
        executor_pod_queue_target_length: int,
        executor_pod_name_prefix: str,
    ) -> None:
        self.kubectl = kubectl
        self.executor_image = executor_image
        self.container_resources = container_resources
        self.file_storage = file_storage
        self.executor_pod_spec_extra = executor_pod_spec_extra
        self.self_pod = None
        self.executor_pod_queue_target_length = executor_pod_queue_target_length
        self.executor_pod_queue_spawning_count = 0
        self.executor_pod_queue = collections.deque()
        self.executor_pod_name_prefix = executor_pod_name_prefix

    @retry(
        retry=retry_if_exception_type(RuntimeError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10),
    )
    @validate_call
    async def execute(
        self,
        source_code: str,
        files: Mapping[AbsolutePath, Hash] = {},
        env: Mapping[str, str] = {},
        chat_id: str | None = None,
        persistent_workspace: bool = False,
    ) -> Result:
        if chat_id is None:
            chat_id = "default"

        async with self.executor_pod() as executor_pod, httpx.AsyncClient(
            timeout=60.0
        ) as client:
            executor_pod_ip = executor_pod["status"]["podIP"]

            async def upload_file(path_: str, file_hash: str):
                async with self.file_storage.reader(
                    file_hash, chat_id, os.path.basename(path_)
                ) as fh:
                    return await client.put(
                        f"http://{executor_pod_ip}:8000/workspace/{path_.removeprefix('/workspace/')}",
                        data=fh,
                    )
            logger.info("Uploading %s files to executor pod", len(files))
            await asyncio.gather(*(upload_file(p, h) for p, h in files.items()))

            logger.info("Requesting code execution")
            response = (
                await client.post(
                    f"http://{executor_pod_ip}:8000/execute",
                    json={"source_code": source_code, "env": env},
                )
            ).json()

            stored_files: dict[str, str] = {}
            if persistent_workspace and response["files"]:
                async def download_file(file_path: str):
                    filename = os.path.basename(file_path)
                    async with self.file_storage.writer(
                        filename, chat_id
                    ) as stored_file, client.stream(
                        "GET",
                        f"http://{executor_pod_ip}:8000/workspace/{file_path.removeprefix('/workspace/')}",
                    ) as pod_file:
                        pod_file.raise_for_status()
                        async for chunk in pod_file.aiter_bytes():
                            await stored_file.write(chunk)

                        from code_interpreter.utils.file_meta import register

                        register(
                            file_hash=stored_file.hash,
                            chat_id=chat_id,
                            filename=filename,
                            max_downloads=config.global_max_downloads,
                        )

                        return file_path, stored_file.hash

                logger.info("Collecting %s changed files", len(response["files"]))
                stored_files = {
                    p: h
                    for p, h in await asyncio.gather(
                        *(download_file(p) for p in response["files"])
                    )
                }

            return KubernetesCodeExecutor.Result(
                stdout=response["stdout"],
                stderr=response["stderr"],
                exit_code=response["exit_code"],
                files=stored_files,
                chat_id=chat_id,
            )

    async def fill_executor_pod_queue(self):
        count_to_spawn = (
            self.executor_pod_queue_target_length
            - len(self.executor_pod_queue)
            - self.executor_pod_queue_spawning_count
        )
        if count_to_spawn <= 0:
            return
        logger.info(
            "Extending executor pod queue to target length %s, current queue length: %s, already spawning: %s, to spawn: %s",
            self.executor_pod_queue_target_length,
            len(self.executor_pod_queue),
            self.executor_pod_queue_spawning_count,
            count_to_spawn,
        )
        self.executor_pod_queue_spawning_count += count_to_spawn

        spawned_pods = 0
        for pod_task in asyncio.as_completed(
            asyncio.create_task(self.spawn_executor_pod())
            for _ in range(count_to_spawn)
        ):
            try:
                self.executor_pod_queue.append(await pod_task)
                spawned_pods += 1
            except Exception:
                logger.exception("Failed to spawn executor pod")
            finally:
                self.executor_pod_queue_spawning_count -= 1
        logger.info(
            "Executor pod queue extended, spawned: %s, failed to spawn: %s, current queue length: %s, still spawning: %s",
            spawned_pods,
            count_to_spawn - spawned_pods,
            len(self.executor_pod_queue),
            self.executor_pod_queue_spawning_count,
        )

    @retry(
        retry=retry_if_exception_type(RuntimeError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10),
    )
    async def spawn_executor_pod(self):
        if self.self_pod is None:
            self.self_pod = await self.kubectl.get("pod", os.environ["HOSTNAME"])

        name = self.executor_pod_name_prefix + "".join(
            random.choice(string.ascii_lowercase + string.digits) for _ in range(6)
        )

        try:
            await self.kubectl.create(
                filename="-",
                input={
                    "apiVersion": "v1",
                    "kind": "Pod",
                    "metadata": {
                        "name": name,
                        "ownerReferences": [
                            {
                                "apiVersion": "v1",
                                "kind": "Pod",
                                "name": self.self_pod["metadata"]["name"],
                                "uid": self.self_pod["metadata"]["uid"],
                                "controller": True,
                                "blockOwnerDeletion": False,
                            }
                        ],
                    },
                    "spec": {
                        "containers": [
                            {
                                "name": "executor",
                                "image": self.executor_image,
                                "resources": self.container_resources,
                                "ports": [{"containerPort": 8000}],
                            }
                        ],
                        **self.executor_pod_spec_extra,
                    },
                },
            )
            return await self.kubectl.wait(
                "pod", name, _for="condition=Ready", timeout="60s"
            )
        except Exception:
            try:
                await self.kubectl.delete("pod", name)
            finally:
                raise RuntimeError("Failed to spawn the pod")

    @asynccontextmanager
    async def executor_pod(self) -> AsyncGenerator[dict, None]:
        pod = (
            self.executor_pod_queue.popleft()
            if self.executor_pod_queue
            else await self.spawn_executor_pod()
        )
        asyncio.create_task(self.fill_executor_pod_queue())
        try:
            logger.info("Grabbing executor pod %s", pod["metadata"]["name"])
            yield pod
        finally:
            logger.info("Removing used executor pod %s", pod["metadata"]["name"])
            asyncio.create_task(self.kubectl.delete("pod", pod["metadata"]["name"]))
