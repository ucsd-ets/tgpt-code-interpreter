from datetime import timedelta
import datetime
import io
from pathlib import Path
import time
from typing import Iterator
from unittest.mock import patch

import httpx
import pytest

from code_interpreter.config import Config
from code_interpreter.utils.file_meta import cleanup_expired_files


class _FakeLargeFile:
    """Streams `total_bytes` zeros; never loads whole payload in RAM."""
    CHUNK = b"0" * 8192

    def __init__(self, total_bytes: int):
        self.remaining = total_bytes

    def read(self, size: int = -1) -> bytes:
        if self.remaining <= 0:
            return b""
        size = self.remaining if size < 0 or size > self.remaining else size
        self.remaining -= size
        reps, rem = divmod(size, len(self.CHUNK))
        return self.CHUNK * reps + self.CHUNK[:rem]


@pytest.fixture
def http_client():
    return httpx.Client(base_url="http://localhost:50081")


@pytest.fixture
def config():
    return Config()


def _upload_payload(fname: str, body: str) -> str:
    return f"from pathlib import Path; Path({fname!r}).write_text({body!r})"


@pytest.fixture
def setup_test_files(http_client):
    fileset = {
        "test_file1.txt": {"chat": "test_chat_1", "max_downloads": 2},
        "test_file2.txt": {"chat": "test_chat_1", "max_downloads": 1},
        "test_file3.txt": {"chat": "test_chat_2", "max_downloads": None},
        "dummy.pdf":      {"chat": "test_chat_2", "max_downloads": None},
        "image.png":      {"chat": "test_chat_2", "max_downloads": None},
        "code.py":        {"chat": "test_chat_2", "max_downloads": None},
    }

    hashes, contents = {}, {}

    for fname, meta in fileset.items():
        chat = meta["chat"]
        body = f"content of {fname}"
        contents[fname] = body

        req = {
            "source_code": _upload_payload(fname, body),
            "chat_id": chat,
            "persistent_workspace": True,
            **({"max_downloads": meta["max_downloads"]} if meta["max_downloads"] is not None else {}),
        }
        resp = http_client.post("/v1/execute", json=req)
        assert resp.status_code == 200
        hashes[fname] = resp.json()["files"][f"/workspace/{fname}"]

    return hashes, contents


@pytest.fixture
def _persist_file(http_client):
    chat, fname, body = "blackbox_chat", "hello.txt", "hello-black-box!"
    resp = http_client.post(
        "/v1/execute",
        json={
            "chat_id": chat,
            "source_code": _upload_payload(fname, body),
            "persistent_workspace": True,
        },
    )
    assert resp.status_code == 200
    return chat, resp.json()["files"][f"/workspace/{fname}"], fname, body


# ---------- /v1/download ---------------------------------------------------


def test_download_with_limits(http_client, setup_test_files):
    hashes, contents = setup_test_files
    for _ in range(2):
        r = http_client.post(
            "/v1/download",
            json=dict(chat_id="test_chat_1",
                      file_hash=hashes["test_file1.txt"],
                      filename="test_file1.txt"),
        )
        assert r.status_code == 200 and r.text == contents["test_file1.txt"]

    r = http_client.post(
        "/v1/download",
        json=dict(chat_id="test_chat_1",
                  file_hash=hashes["test_file1.txt"],
                  filename="test_file1.txt"),
    )
    assert r.status_code == 404


def test_single_download_limit(http_client, setup_test_files):
    hashes, contents = setup_test_files
    ok = http_client.post(
        "/v1/download",
        json=dict(chat_id="test_chat_1",
                  file_hash=hashes["test_file2.txt"],
                  filename="test_file2.txt"),
    )
    assert ok.status_code == 200 and ok.text == contents["test_file2.txt"]

    fail = http_client.post(
        "/v1/download",
        json=dict(chat_id="test_chat_1",
                  file_hash=hashes["test_file2.txt"],
                  filename="test_file2.txt"),
    )
    assert fail.status_code == 404


def test_unlimited_downloads(http_client, setup_test_files):
    hashes, contents = setup_test_files
    for _ in range(3):
        r = http_client.post(
            "/v1/download",
            json=dict(chat_id="test_chat_2",
                      file_hash=hashes["test_file3.txt"],
                      filename="test_file3.txt"),
        )
        assert r.status_code == 200 and r.text == contents["test_file3.txt"]


def test_content_type_detection(http_client, setup_test_files):
    hashes, _ = setup_test_files
    mime = {"dummy.pdf": "application/pdf", "image.png": "image/png",
            "code.py": "text/x-python"}
    for fname, m in mime.items():
        r = http_client.post(
            "/v1/download",
            json=dict(chat_id="test_chat_2",
                      file_hash=hashes[fname],
                      filename=fname),
        )
        assert r.status_code == 200 and r.headers["Content-Type"].startswith(m)


def test_download_errors(http_client, setup_test_files):
    hashes, _ = setup_test_files
    bad_cases = [
        dict(chat_id="test_chat_1", file_hash="nonexistent", filename="x"),
        dict(chat_id="wrong_chat",  file_hash=hashes["test_file1.txt"], filename="test_file1.txt"),
        dict(chat_id="test_chat_1", file_hash="BADHASH",   filename="test_file1.txt"),
    ]
    for payload in bad_cases:
        assert http_client.post("/v1/download", json=payload).status_code == 404


def test_no_persist(http_client):
    src = "from pathlib import Path; Path('file.txt').write_text('Hello')"
    resp = http_client.post("/v1/execute",
                            json=dict(source_code=src,
                                      persistent_workspace=False))
    assert resp.status_code == 200 and not resp.json()["files"]

    read = http_client.post(
        "/v1/execute",
        json=dict(source_code="open('file.txt').read()"))
    assert read.json()["exit_code"] == 1

def test_execute_with_download_limit(http_client):
    """Test executing code that creates a file with download limit"""
    chat_id = "execute_limit_test_chat"
    filename = "execute_limited.txt"
    content = "File created by execution with limit"
    max_downloads = 2
    
    execute_payload = {
        "source_code": f'from pathlib import Path; Path("{filename}").write_text("{content}")',
        "chat_id": chat_id,
        "persistent_workspace": True,
        "max_downloads": max_downloads
    }
    
    execute_response = http_client.post("/v1/execute", json=execute_payload)
    assert execute_response.status_code == 200
    
    # Get the file hash
    result = execute_response.json()
    file_path = f"/workspace/{filename}"
    assert file_path in result["files"], f"Expected {file_path} in {result['files']}"
    file_hash = result["files"][file_path]
    
    # Check metadata is returned
    assert "files_metadata" in result
    assert file_path in result["files_metadata"]
    assert result["files_metadata"][file_path]["remaining_downloads"] == max_downloads
    
    download_payload = {
        "chat_id": chat_id,
        "file_hash": file_hash,
        "filename": filename
    }
    
    for i in range(max_downloads):
        download_response = http_client.post("/v1/download", json=download_payload)
        assert download_response.status_code == 200, f"Failed on download {i+1}"
        assert download_response.text == content
    
    # Try to download one more time - should fail
    exceeded_response = http_client.post("/v1/download", json=download_payload)
    assert exceeded_response.status_code == 404


def test_execute_with_expiry_time(http_client):
    """File created via /v1/execute should auto-expire after ~3 s."""
    chat_id  = "exec_expiry_chat"
    filename = "auto_expire_exec.txt"
    content  = "short-lived file"

    rsp = http_client.post(
        "/v1/execute",
        json={
            "source_code": f'from pathlib import Path; Path("{filename}").write_text("{content}")',
            "chat_id": chat_id,
            "persistent_workspace": True,
            "expires_in": "3s",
        },
    )
    assert rsp.status_code == 200
    file_hash = rsp.json()["files"][f"/workspace/{filename}"]

    payload = {"chat_id": chat_id, "file_hash": file_hash, "filename": filename}
    assert http_client.post("/v1/download", json=payload).status_code == 200

    time.sleep(5)
    assert http_client.post("/v1/download", json=payload).status_code == 404


# ---------- /v1/upload -----------------------------------------------------

def test_upload_too_large(http_client):
    two_gib = 2_147_483_648
    files = {
        "chat_id": (None, "huge_file_chat"),
        "upload": ("huge.bin", _FakeLargeFile(two_gib), "application/octet-stream"),
    }
    r = http_client.post("/v1/upload", files=files, timeout=120)
    assert r.status_code == 413 and "File too large" in r.text


def test_upload_then_execute(http_client):
    chat, csv, body = "upload_exec_chat", "data.csv", "a,b\n1,2\n"
    files = {"chat_id": (None, chat),
             "upload": (csv, io.BytesIO(body.encode()), "text/csv")}
    up = http_client.post("/v1/upload", files=files)
    assert up.status_code == 200
    csv_hash = up.json()["file_hash"]

    payload = {
        "chat_id": chat,
        "source_code": "import pandas as pd, sys; print(pd.read_csv('data.csv')['a'].sum())",
        "files": {f"/workspace/{csv}": csv_hash},
    }
    ex = http_client.post("/v1/execute", json=payload)
    assert ex.status_code == 200 and ex.json()["stdout"].strip() == "1"

def test_upload_then_download(http_client):
    # Prepare test data
    chat_id = "upload_download_test_chat"
    filename = "test_document.txt"
    content = "This is a test document for direct upload/download test."
    
    # Upload the file
    files = {
        "chat_id": (None, chat_id),
        "upload": (filename, io.BytesIO(content.encode()), "text/plain"),
    }
    upload_response = http_client.post("/v1/upload", files=files)
    assert upload_response.status_code == 200
    
    # Extract file hash from response
    upload_data = upload_response.json()
    assert upload_data["chat_id"] == chat_id
    assert upload_data["filename"] == filename
    file_hash = upload_data["file_hash"]
    
    # Now download the file directly
    download_payload = {
        "chat_id": chat_id,
        "file_hash": file_hash,
        "filename": filename
    }
    download_response = http_client.post("/v1/download", json=download_payload)
    
    # Verify download was successful and content matches
    assert download_response.status_code == 200
    assert download_response.text == content
    assert download_response.headers["Content-Type"].startswith("text/plain")
    assert "Content-Disposition" in download_response.headers
    assert filename in download_response.headers["Content-Disposition"]
    
    # Verify download counter works (assuming default is 0 = unlimited)
    # Try downloading again
    second_download = http_client.post("/v1/download", json=download_payload)
    assert second_download.status_code == 200
    assert second_download.text == content

def test_upload_with_download_limit(http_client):
    """Test uploading a file with max_downloads specified"""
    # Prepare test data
    chat_id = "upload_limit_test_chat"
    filename = "limited_file.txt"
    content = "This file has a download limit of 2"
    max_downloads = 2
    
    # Upload the file with a limit
    files = {
        "chat_id": (None, chat_id),
        "upload": (filename, io.BytesIO(content.encode()), "text/plain"),
        "max_downloads": (None, str(max_downloads)),
    }
    upload_response = http_client.post("/v1/upload", files=files)
    assert upload_response.status_code == 200
    
    # Extract file hash from response
    upload_data = upload_response.json()
    assert upload_data["chat_id"] == chat_id
    assert upload_data["filename"] == filename
    assert "metadata" in upload_data
    assert upload_data["metadata"]["remaining_downloads"] == max_downloads
    file_hash = upload_data["file_hash"]
    
    # Download the file the allowed number of times
    download_payload = {
        "chat_id": chat_id,
        "file_hash": file_hash,
        "filename": filename
    }
    
    for i in range(max_downloads):
        download_response = http_client.post("/v1/download", json=download_payload)
        assert download_response.status_code == 200, f"Failed on download {i+1}"
        assert download_response.text == content
    
    # Try to download one more time - should fail
    exceeded_response = http_client.post("/v1/download", json=download_payload)
    assert exceeded_response.status_code == 404


def test_upload_with_expiry_time(http_client):
    """File uploaded with expires_in should vanish after interval."""
    chat_id  = "upload_expiry_chat"
    filename = "auto_expire_upload.txt"
    content  = "ephemeral upload"

    up = http_client.post(
        "/v1/upload",
        files={
            "chat_id":       (None, chat_id),
            "upload":        (filename, io.BytesIO(content.encode()), "text/plain"),
            "expires_in":    (None, "3s"),
        },
    )
    assert up.status_code == 200
    file_hash = up.json()["file_hash"]

    payload = {"chat_id": chat_id, "file_hash": file_hash, "filename": filename}
    assert http_client.post("/v1/download", json=payload).status_code == 200

    time.sleep(4)
    assert http_client.post("/v1/download", json=payload).status_code == 404

def test_upload_metadata_response(http_client):
    chat_id = "metadata_test_chat"
    filename = "metadata_test_file.txt"
    content = "Testing metadata in upload response"
    max_downloads = 5
    expires_in = "7d"

    files = {
        "chat_id":      (None, chat_id),
        "upload":       (filename, io.BytesIO(content.encode()), "text/plain"),
        "max_downloads":(None, str(max_downloads)),
        "expires_in":   (None, expires_in),
    }

    upload_response = http_client.post("/v1/upload", files=files)
    assert upload_response.status_code == 200
    data = upload_response.json()

    assert data["metadata"]["remaining_downloads"] == max_downloads
    expiry = datetime.datetime.fromisoformat(data["metadata"]["expires_at"])
    delta = expiry - datetime.datetime.now()
    assert 6 <= delta.days <= 7


def test_combine_limit_and_expiry_time(http_client):
    """Time-based expiry should trump a remaining-download quota."""
    chat_id  = "dual_expiry_chat"
    filename = "dual_expire.txt"
    content  = "both count and timer"

    up = http_client.post(
        "/v1/upload",
        files={
            "chat_id":      (None, chat_id),
            "upload":       (filename, io.BytesIO(content.encode()), "text/plain"),
            "max_downloads":(None, "5"),
            "expires_in":   (None, "3s"),
        },
    )
    assert up.status_code == 200
    file_hash = up.json()["file_hash"]

    payload = {"chat_id": chat_id, "file_hash": file_hash, "filename": filename}
    assert http_client.post("/v1/download", json=payload).status_code == 200

    time.sleep(4)
    assert http_client.post("/v1/download", json=payload).status_code == 404


# ---------- /v1/expire -----------------------------------------------------


def test_expire_bad_chat(http_client, _persist_file):
    chat, h, fname, _ = _persist_file
    r = http_client.post("/v1/expire",
                         json=dict(chat_id="WRONG_CHAT", file_hash=h, filename=fname))
    assert r.status_code == 404


def test_expire_bad_hash(http_client, _persist_file):
    chat, _, fname, _ = _persist_file
    r = http_client.post("/v1/expire",
                         json=dict(chat_id=chat, file_hash="bad"*16, filename=fname))
    assert r.status_code == 404


def test_expire_success(http_client, _persist_file):
    chat, h, fname, body = _persist_file

    ok = http_client.post("/v1/download",
                          json=dict(chat_id=chat, file_hash=h, filename=fname))
    assert ok.status_code == 200 and ok.text == body

    exp = http_client.post("/v1/expire",
                           json=dict(chat_id=chat, file_hash=h, filename=fname))
    assert exp.status_code == 200

    assert http_client.post("/v1/download",
                            json=dict(chat_id=chat, file_hash=h, filename=fname)
                            ).status_code == 404

    cleanup_expired_files()
    assert http_client.post("/v1/download",
                            json=dict(chat_id=chat, file_hash=h, filename=fname)
                            ).status_code == 404

@pytest.mark.timeout(180)
def test_workspace_quota_enforced(http_client: httpx.Client):
    """
    Executor pod should hit 'No space left on device' (ENOSPC) before it
    manages to write more than the 1 GiB quota.
    """
    # 1 MiB chunk written 1 200 times  ≈ 1.2 GiB
    source = """
import os, sys
chunk = b"0" * (1024 * 1024)        # 1 MiB
try:
    with open("huge.bin", "wb") as f:
        for i in range(1200):
            f.write(chunk)
    print("UNEXPECTED SUCCESS")      # should never hit
except OSError as e:
    # Propagate errno so the interpreter exits non-zero
    print("Caught:", e)
    sys.exit(1 if e.errno != 0 else 2)
"""
    r = http_client.post("/v1/execute", json={"source_code": source})
    assert r.status_code == 200

    payload = r.json()
    print(str(payload))
    # Quota breach should lead to non-zero exit and ENOSPC in stderr
    assert payload["exit_code"] != 0, "code ran to completion (quota not enforced?)"
    assert "No space left" in payload["stderr"] or "No space left" in payload["stdout"]