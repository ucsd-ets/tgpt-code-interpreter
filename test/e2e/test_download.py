# test/e2e/test_download.py
import base64
import pytest
import httpx
import os
import sqlite3
import shutil
import tempfile
from pathlib import Path
from code_interpreter.config import Config
from code_interpreter.utils.file_meta import register, check_and_decrement

@pytest.fixture
def setup_test_files():
    # Create temporary test directory
    config = Config()
    stor = config.file_storage_path
    
    # Override config for testing
    config.require_chat_id = True
    config.global_max_downloads = 2
    
    # Create test files
    test_files = {
        "test_file1": b"This is test file 1 content",
        "test_file2": b"This is test file 2 content", 
        "test_file3": b"This is test file 3 content",
    }
    
    for file_hash, content in test_files.items():
        file_path = os.path.join(stor, file_hash)
        with open(file_path, "wb") as f:
            f.write(content)
            
    # Register files in database
    register("test_file1", "test_chat_1", 2)  # 2 downloads allowed
    register("test_file2", "test_chat_1", 1)  # 1 download allowed
    register("test_file3", "test_chat_2", 0)  # unlimited downloads
    
    yield stor, test_files

@pytest.fixture
def http_client():
    base_url = "http://localhost:50081"
    return httpx.Client(base_url=base_url)

def test_successful_download(http_client, setup_test_files):
    temp_dir, test_files = setup_test_files
    
    # Request a file download
    response = http_client.post(
        "/v1/download", 
        json={
            "chat_id": "test_chat_1",
            "file_hash": "test_file1",
            "filename": "myfile.txt"
        }
    )
    
    assert response.status_code == 200
    response_data = response.json()
    
    # Verify response format
    assert response_data["filename"] == "myfile.txt"
    assert response_data["content_type"] == "text/plain"
    
    # Decode and verify content
    decoded_content = base64.b64decode(response_data["content_base64"]).decode('utf-8')
    assert decoded_content == "This is test file 1 content"
    
    # Verify counter decremented
    conn = sqlite3.connect(os.path.join(temp_dir, "file_mgmt_db.sqlite3"))
    remaining = conn.execute(
        "SELECT remaining FROM files WHERE file_hash = 'test_file1'"
    ).fetchone()[0]
    conn.close()
    
    assert remaining == 1  # Started with 2, now should be 1

def test_wrong_chat_id(http_client, setup_test_files):
    temp_dir, test_files = setup_test_files
    
    # Try with wrong chat ID
    response = http_client.post(
        "/v1/download", 
        json={
            "chat_id": "wrong_chat_id",
            "file_hash": "test_file1",
            "filename": "myfile.txt"
        }
    )
    
    assert response.status_code == 403
    assert "Unauthorized access" in response.json()["detail"]

def test_file_not_found(http_client, setup_test_files):
    temp_dir, test_files = setup_test_files
    
    # Try with non-existent file
    response = http_client.post(
        "/v1/download", 
        json={
            "chat_id": "test_chat_1",
            "file_hash": "nonexistent_file",
            "filename": "myfile.txt"
        }
    )
    
    assert response.status_code == 404
    assert "not found" in response.json()["detail"]

def test_download_limit_reached(http_client, setup_test_files):
    temp_dir, test_files = setup_test_files
    
    # First download should succeed
    response1 = http_client.post(
        "/v1/download", 
        json={
            "chat_id": "test_chat_1",
            "file_hash": "test_file2",
            "filename": "myfile.txt"
        }
    )
    assert response1.status_code == 200
    
    # Second download should fail (limit was 1)
    response2 = http_client.post(
        "/v1/download", 
        json={
            "chat_id": "test_chat_1",
            "file_hash": "test_file2",
            "filename": "myfile.txt"
        }
    )
    assert response2.status_code == 410
    assert "limit reached" in response2.json()["detail"]
    
    # Check that record was deleted
    conn = sqlite3.connect(os.path.join(temp_dir, "file_mgmt_db.sqlite3"))
    result = conn.execute(
        "SELECT * FROM files WHERE file_hash = 'test_file2'"
    ).fetchone()
    conn.close()
    
    assert result is None  # Record should be deleted after downloads exhausted

def test_unlimited_downloads(http_client, setup_test_files):
    temp_dir, test_files = setup_test_files
    
    # Do multiple downloads of the unlimited file
    for _ in range(5):
        response = http_client.post(
            "/v1/download", 
            json={
                "chat_id": "test_chat_2",
                "file_hash": "test_file3",
                "filename": "myfile.txt"
            }
        )
        assert response.status_code == 200
    
    # Verify the unlimited setting is preserved
    conn = sqlite3.connect(os.path.join(temp_dir, "file_mgmt_db.sqlite3"))
    remaining = conn.execute(
        "SELECT remaining FROM files WHERE file_hash = 'test_file3'"
    ).fetchone()[0]
    conn.close()
    
    assert remaining is None  # Should still be None (unlimited)

def test_content_type_detection(http_client, setup_test_files):
    temp_dir, test_files = setup_test_files
    
    # Test different file extensions
    file_types = {
        "document.pdf": "application/pdf",
        "image.png": "image/png",
        "code.py": "text/x-python",
        "data.json": "application/json",
        "spreadsheet.xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }
    
    for filename, expected_type in file_types.items():
        response = http_client.post(
            "/v1/download", 
            json={
                "chat_id": "test_chat_1",
                "file_hash": "test_file1",
                "filename": filename
            }
        )
        
        assert response.status_code == 200
        assert response.json()["content_type"] == expected_type