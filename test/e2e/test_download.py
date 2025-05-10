# test/e2e/test_download.py
import pytest
import httpx


@pytest.fixture
def http_client():
    return httpx.Client(
        base_url="http://localhost:50081",
    )


@pytest.fixture
def setup_test_files(http_client):
    to_create = {
        "test_file1.txt": {"chat": "test_chat_1", "limit": 2},
        "test_file2.txt": {"chat": "test_chat_1", "limit": 1},
        "test_file3.txt": {"chat": "test_chat_2", "limit": None},
        "dummy.pdf": {"chat": "test_chat_2", "limit": None},
        "image.png": {"chat": "test_chat_2", "limit": None},
        "code.py": {"chat": "test_chat_2", "limit": None},
    }

    hashes = {}
    contents = {}

    for fname, meta in to_create.items():
        chat_id = meta["chat"]
        limit = meta["limit"]
        content = f"content of {fname}"
        contents[fname] = content

        payload = {
            "source_code": (
                f"from pathlib import Path\n"
                f"Path({fname!r}).write_text({content!r})"
            ),
            "chat_id": chat_id,
            "workspace_persistence": True,
        }
        if limit is not None:
            payload["limit"] = limit

        resp = http_client.post("/v1/execute", json=payload)
        assert resp.status_code == 200, resp.text
        result = resp.json()
        hashes[fname] = result["files"][f"/workspace/{fname}"]

    return hashes, contents


def test_download_with_limits(http_client, setup_test_files):
    hashes, contents = setup_test_files

    for _ in range(2):
        r = http_client.post(
            "/v1/download",
            json={
                "chat_id": "test_chat_1",
                "file_hash": hashes["test_file1.txt"],
                "filename": "test_file1.txt",
            },
        )
        assert r.status_code == 200
        assert r.text == contents["test_file1.txt"]

    r = http_client.post(
        "/v1/download",
        json={
            "chat_id": "test_chat_1",
            "file_hash": hashes["test_file1.txt"],
            "filename": "test_file1.txt",
        },
    )
    assert r.status_code == 404


def test_single_download_limit(http_client, setup_test_files):
    hashes, contents = setup_test_files

    first = http_client.post(
        "/v1/download",
        json={
            "chat_id": "test_chat_1",
            "file_hash": hashes["test_file2.txt"],
            "filename": "test_file2.txt",
        },
    )
    assert first.status_code == 200
    assert first.text == contents["test_file2.txt"]

    second = http_client.post(
        "/v1/download",
        json={
            "chat_id": "test_chat_1",
            "file_hash": hashes["test_file2.txt"],
            "filename": "test_file2.txt",
        },
    )
    assert second.status_code == 404


def test_unlimited_downloads(http_client, setup_test_files):
    hashes, contents = setup_test_files

    for _ in range(3):
        r = http_client.post(
            "/v1/download",
            json={
                "chat_id": "test_chat_2",
                "file_hash": hashes["test_file3.txt"],
                "filename": "test_file3.txt",
            },
        )
        assert r.status_code == 200
        assert r.text == contents["test_file3.txt"]


def test_content_type_detection(http_client, setup_test_files):
    hashes, _ = setup_test_files

    expected_types = {
        "dummy.pdf": "application/pdf",
        "image.png": "image/png",
        "code.py": "text/x-python",
    }

    for fname, mimetype in expected_types.items():
        r = http_client.post(
            "/v1/download",
            json={
                "chat_id": "test_chat_2",
                "file_hash": hashes[fname],
                "filename": fname,
            },
        )
        assert r.status_code == 200
        assert r.headers["Content-Type"].startswith(mimetype)


def test_file_not_found(http_client, setup_test_files):
    _ = setup_test_files
    r = http_client.post(
        "/v1/download",
        json={
            "chat_id": "test_chat_1",
            "file_hash": "nonexistent_hash",
            "filename": "doesnt_matter.txt",
        },
    )
    assert r.status_code == 404


def test_wrong_chat_id(http_client, setup_test_files):
    hashes, _ = setup_test_files

    r = http_client.post(
        "/v1/download",
        json={
            "chat_id": "wrong_chat",
            "file_hash": hashes["test_file1.txt"],
            "filename": "test_file1.txt",
        },
    )
    assert r.status_code == 404


def test_bad_hash(http_client, setup_test_files):
    _ = setup_test_files
    r = http_client.post(
        "/v1/download",
        json={
            "chat_id": "test_chat_1",
            "file_hash": "BADHASH",
            "filename": "test_file1.txt",
        },
    )
    assert r.status_code == 404

def test_no_persist(http_client, setup_test_files):
    file_content = "Hello, World!"

    response = http_client.post(
        "/v1/execute",
        json={
            "source_code": """
with open('file.txt', 'w') as f:
    f.write("Hello, World!")
""",
            "files": {},
            "workspace_persistence": False,
        },
    )

    assert response.status_code == 200
    response_json = response.json()
    assert response_json["exit_code"] == 0
    assert not response_json["files"].keys()

    response = http_client.post(
        "/v1/execute",
        json={
            "source_code": """
with open('file.txt', 'r') as f:
    print(f.read())
""",
        },
    )

    assert response.status_code == 200
    response_json = response.json()
    assert response_json["exit_code"] == 1
    assert "No such file or directory" in response_json['stderr']