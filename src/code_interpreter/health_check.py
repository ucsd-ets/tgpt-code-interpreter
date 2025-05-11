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

import httpx
from code_interpreter.config import Config

def health_check():
    """
    Performs a health check using either gRPC (if enabled) or HTTP API.
    Executes a simple arithmetic operation and verifies the result.
    """
    config = Config()
    
    if getattr(config, 'grpc_enabled', True):
        try:
            return grpc_health_check(config)
        except Exception as e:
            print(f"gRPC health check failed: {e}")
            print("Falling back to HTTP health check...")
    
    return http_health_check(config)


def grpc_health_check(config):
    import grpc
    from proto.code_interpreter.v1.code_interpreter_service_pb2 import ExecuteRequest
    from proto.code_interpreter.v1.code_interpreter_service_pb2_grpc import CodeInterpreterServiceStub

    if (
        not config.grpc_tls_cert
        or not config.grpc_tls_cert_key
        or not config.grpc_tls_ca_cert
    ):
        channel = grpc.insecure_channel(config.grpc_listen_addr)
    else:
        channel = grpc.secure_channel(
            config.grpc_listen_addr,
            grpc.ssl_server_credentials(
                private_key_certificate_chain_pairs=[
                    (config.grpc_tls_cert_key, config.grpc_tls_cert)
                ],
                root_certificates=config.grpc_tls_ca_cert,
            ),
        )

    result = CodeInterpreterServiceStub(channel).Execute(
        ExecuteRequest(source_code="print(21 * 2)"),
        timeout=30,
    )
    
    assert result.stdout == "42\n", f"Expected '42\n', got '{result.stdout}'"
    assert result.exit_code == 0, f"Expected exit code 0, got {result.exit_code}"
    
    print("gRPC health check passed successfully!")
    return True


def http_health_check(config):
    http_base_url = f"http://{config.http_listen_addr}"
    if ":" not in http_base_url:
        http_base_url = f"http://localhost:{config.http_listen_addr}"
    
    payload = {
        "source_code": "print(21 * 2)",
        "chat_id": "health_check"
    }
    
    response = httpx.post(
        f"{http_base_url}/v1/execute", 
        json=payload,
        timeout=30.0
    )
    
    # Validate the response
    response.raise_for_status()
    result = response.json()
    
    assert result["stdout"] == "42\n", f"Expected '42\n', got '{result['stdout']}'"
    assert result["exit_code"] == 0, f"Expected exit code 0, got {result['exit_code']}"
    
    print("HTTP health check passed successfully!")
    return True


if __name__ == "__main__":
    health_check()