<h1 align="left">TritonGPT (based on BeeAI) Code Interpreter</h1>

<p align="left">
TritonGPT (based on BeeAI) Code Interpreter is a powerful HTTP service built to enable LLMs to execute arbitrary Python code. Engineered with safety and reproducibility at the core, this service is designed to seamlessly integrate with your applications.
</p>

> [!NOTE]
> This project includes submodules. Clone it using:
> `git clone --recurse-submodules`.
> 
> If you've already cloned it, initialize submodules with:
> `git submodule update --init`.

You can quickly set up TritonGPT Code Interpreter locally without needing to install Python or Poetry, as everything runs inside Docker and Kubernetes.

## Quick Start

1. **Install Rancher Desktop:** Download and install [Rancher Desktop](https://rancherdesktop.io/), a local Docker and Kubernetes distribution.
   > [!WARNING]
   > If you use a different local Docker/Kubernetes environment, you may need additional steps to make locally built images available to Kubernetes.

2. **Verify kubectl Context:** Ensure your kubectl is pointing to the correct context.

3. **Run TritonGPT Code Interpreter:** Use one of the following commands:
   - **Use pre-built images**: `bash scripts/run-pull.sh` (recommended if you made no changes)
   - **Build images locally**: `bash scripts/run-build.sh`
   > [!WARNING]
   > Building images locally may take considerable time on slower machines.

4. **Verify the service is running**: Run `python -m code_interpreter.health_check`

## HTTP API Reference

The service exposes the following HTTP endpoints:

### Execute Code

Executes arbitrary Python code in a sandboxed environment, with on-the-fly installation of any missing libraries.

**Endpoint:** `POST /v1/execute`

**Request Body:**
```json
{
    "source_code": "print('Hello, World!')",
    "files": {
        "/workspace/example.csv": "file_hash_123"
    },
    "env": {
        "ENV_VAR": "value"
    },
    "chat_id": "unique_session_id",
    "persistent_workspace": true,
    "max_downloads": 5,
    "expires_days": 7,
    "expires_seconds": 3600
}
```

| Parameter | Type | Description |
|-----------|------|-------------|
| `source_code` | string | Python code to execute |
| `files` | object | (Optional) Map of file paths to their hashes |
| `env` | object | (Optional) Environment variables to set |
| `chat_id` | string | (Required) Unique identifier for the session |
| `persistent_workspace` | boolean | (Optional) Whether to persist files created during execution |
| `max_downloads` | integer | (Optional) Maximum number of downloads allowed for output files |
| `expires_days` | integer | (Optional) Number of days until files expire |
| `expires_seconds` | integer | (Optional) Number of seconds until files expire |

**Response:**
```json
{
    "stdout": "Hello, World!\n",
    "stderr": "",
    "exit_code": 0,
    "files": {
        "/workspace/output.txt": "hash_of_file"
    },
    "files_metadata": {
        "/workspace/output.txt": {
            "remaining_downloads": 5,
            "expires_at": "2025-05-19T12:34:56"
        }
    },
    "chat_id": "unique_session_id"
}
```

### Upload File

Upload a file to be used in code execution.

**Endpoint:** `POST /v1/upload`

**Form Data:**
- `chat_id`: (Required) Unique identifier for the session
- `upload`: (Required) File to upload
- `max_downloads`: (Optional) Maximum number of downloads allowed
- `expires_days`: (Optional) Number of days until file expires
- `expires_seconds`: (Optional) Number of seconds until file expires

**Response:**
```json
{
    "file_hash": "hash_of_uploaded_file",
    "filename": "example.csv",
    "chat_id": "unique_session_id",
    "metadata": {
        "remaining_downloads": 5,
        "expires_at": "2025-05-19T12:34:56"
    }
}
```

### Download File

Download a file by its hash.

**Endpoint:** `POST /v1/download`

**Request Body:**
```json
{
    "chat_id": "unique_session_id",
    "file_hash": "hash_of_file",
    "filename": "example.csv"
}
```

**Response:** The file content with appropriate Content-Type and Content-Disposition headers.

### Expire File

Manually expire a file to prevent further downloads.

**Endpoint:** `POST /v1/expire`

**Request Body:**
```json
{
    "chat_id": "unique_session_id",
    "file_hash": "hash_of_file",
    "filename": "example.csv"
}
```

**Response:**
```json
{
    "success": true
}
```

### Parse Custom Tool

Parse a custom tool definition and return its metadata.

**Endpoint:** `POST /v1/parse-custom-tool`

**Request Body:**
```json
{
    "tool_source_code": "def my_tool(param1: str, param2: int) -> str:\n    \"\"\"Tool description\n    :param param1: Description of param1\n    :param param2: Description of param2\n    :return: Description of return value\n    \"\"\"\n    return f\"Result: {param1}, {param2}\""
}
```

**Response:**
```json
{
    "tool_name": "my_tool",
    "tool_input_schema_json": "{\"$schema\":\"http://json-schema.org/draft-07/schema#\",\"type\":\"object\",\"properties\":{...}}",
    "tool_description": "Tool description\n\nReturns: Description of return value"
}
```

### Execute Custom Tool

Execute a custom tool with the provided input.

**Endpoint:** `POST /v1/execute-custom-tool`

**Request Body:**
```json
{
    "tool_source_code": "def my_tool(param1: str, param2: int) -> str:\n    return f\"Result: {param1}, {param2}\"",
    "tool_input_json": "{\"param1\": \"hello\", \"param2\": 42}",
    "env": {
        "ENV_VAR": "value"
    }
}
```

**Response:**
```json
{
    "tool_output_json": "\"Result: hello, 42\""
}
```

## Configuration Options

The service can be configured using environment variables with the `APP_` prefix. Here are the key configuration options:

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `APP_GRPC_ENABLED` | boolean | `false` | Enable/disable gRPC server |
| `APP_GRPC_LISTEN_ADDR` | string | `0.0.0.0:50051` | Address and port for gRPC server |
| `APP_HTTP_LISTEN_ADDR` | string | `0.0.0.0:50081` | Address and port for HTTP server |
| `APP_SCHEMA_PATH` | string | `""` | Path to JSON schema for validation |
| `APP_EXECUTOR_IMAGE` | string | `localhost/tgpt-code-executor:local` | Docker image for executor pods |
| `APP_FILE_STORAGE_PATH` | string | `/tmp/code_interp` | Path to store files |
| `APP_EXECUTOR_POD_QUEUE_TARGET_LENGTH` | integer | `5` | Number of executor pods to keep ready |
| `APP_EXECUTOR_POD_NAME_PREFIX` | string | `code-executor-` | Prefix for executor pod names |
| `APP_PUBLIC_SPAWN_ENABLED` | boolean | `false` | Allow public access to execution endpoints |
| `APP_REQUIRE_CHAT_ID` | boolean | `true` | Require chat_id parameter for execution |
| `APP_GLOBAL_MAX_DOWNLOADS` | integer | `0` | Default download limit (0 = unlimited) |
| `APP_INTERNAL_HOST_ALLOWLIST` | list | `[]` | Hosts allowed to execute code when public spawn is disabled |
| `APP_INTERNAL_IP_ALLOWLIST` | list | `[]` | IPs allowed to execute code when public spawn is disabled |
| `APP_WORKSPACE_SIZE_LIMIT` | string | `1Gi` | Kubernetes storage limit for /workspace before user receives an out of storage error. |

For TLS configuration:
- `APP_GRPC_TLS_CERT`: TLS certificate content
- `APP_GRPC_TLS_CERT_KEY`: TLS key content
- `APP_GRPC_TLS_CA_CERT`: CA certificate content

Executor pod resources can be configured with:
- `APP_EXECUTOR_CONTAINER_RESOURCES`: JSON-serialized Kubernetes container resources
- `APP_EXECUTOR_POD_SPEC_EXTRA`: JSON-serialized additional fields for pod spec

## Production Setup

To configure TritonGPT Code Interpreter for production:

1. **Configuration**: Use environment variables to override default settings (see Configuration Options above).

2. **Security Considerations**:
   - **THIS APPLICATION IS NOT PROPERLY SANDBOXED! Use a Kubernetes cluster with a secure container runtime (gVisor, Kata Containers, etc.)**
   - Create a service account with appropriate permissions for pod management
   - Ensure the executor image is available in your registry
   - Configure appropriate download limits and expiration settings for files
   - Set `APP_PUBLIC_SPAWN_ENABLED=false` and configure allowlists

3. **File Management**:
   - The service automatically tracks file metadata (download limits, expiration)
   - File objects are cleaned up periodically
   - For production workloads, consider implementing additional cleanup strategies

## Security Features

- Sandboxed code execution in isolated pods
- Download limits for generated files
- File expiration (time-based)
- Chat ID isolation for multi-tenant environments
- Request validation and payload size limits
- IP and host allowlists for controlled access

## Development

### Environment Setup

Use [mise-en-place](https://mise.jdx.dev/) to set up your development environment:

```bash
mise install
poetry install
```

### Testing

```bash
# Start the service
poe run poetry run

# Run tests in another terminal
poe test poetry test test/e2e
```

### Publishing a New Version

```bash
VERSION=...
git checkout main
git pull
poetry version $VERSION
git add pyproject.toml
git commit -m "chore: bump version to v$VERSION"
git tag v$VERSION
git push origin main v$VERSION
```

## Bugs & Support

We use [GitHub Issues](https://github.com/ucsd-ets/tgpt-code-interpreter/issues) for bug tracking. Before filing a new issue, please check if the problem has already been reported.

## License

This project is licensed under the Apache License 2.0 - see the [LICENSE](./LICENSE) file for details.