# test/e2e/test_file_mgmt.py
import io
from pathlib import Path
from typing import Iterator

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
        "test_file1.txt": {"chat": "test_chat_1", "limit": 2},
        "test_file2.txt": {"chat": "test_chat_1", "limit": 1},
        "test_file3.txt": {"chat": "test_chat_2", "limit": None},
        "dummy.pdf":      {"chat": "test_chat_2", "limit": None},
        "image.png":      {"chat": "test_chat_2", "limit": None},
        "code.py":        {"chat": "test_chat_2", "limit": None},
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
            **({"limit": meta["limit"]} if meta["limit"] is not None else {}),
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


# ---------- /v1/upload -----------------------------------------------------


def test_upload_too_large(http_client):
    two_gib = 2_147_483_648
    files = {
        "chat_id": (None, "huge_file_chat"),
        "upload": ("huge.bin", _FakeLargeFile(two_gib), "application/octet-stream"),
    }
    r = http_client.post("/v1/upload", files=files)
    assert r.status_code in {413, 419}


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


# TODO: Proper test: /upload followed by /download directly instead of using execute at all.

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
