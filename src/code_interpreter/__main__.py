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
import aiorun
import uvicorn

from code_interpreter.application_context import ApplicationContext


async def main():
    ctx = ApplicationContext()
    
    http_task = uvicorn.Server(
        uvicorn.Config(
            ctx.http_server,
            host=ctx.config.http_listen_addr.split(":")[0],
            port=int(ctx.config.http_listen_addr.split(":")[1]),
            loop="asyncio",
        )
    ).serve()
    
    tasks = [http_task]
    if ctx.config.grpc_enabled:
        tasks.append(ctx.grpc_server.start(listen_addr=ctx.config.grpc_listen_addr))
    
    await asyncio.gather(*tasks)

aiorun.run(main())
