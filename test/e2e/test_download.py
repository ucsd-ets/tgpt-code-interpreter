import os
import pytest
import httpx
import json
import base64

from code_interpreter.config import Config

@pytest.fixture
def config():
    return Config()

@pytest.fixture
def http_client():
    base_url = "http://localhost:50081"
    return httpx.Client(base_url=base_url)

@pytest.fixture
def setup_test_files(http_client):
    """Create test files and register them in the database"""
    # Setup test files with unique content
    test_files = {
        "test_file1": "This is test file 1 content",
        "test_file2": "This is test file 2 content", 
        "test_file3": "This is test file 3 content"
    }
    
    file_hashes = {}
    
    # Create files and register them in one step
    for file_name, content in test_files.items():
        chat_id = "test_chat_1" if file_name != "test_file3" else "test_chat_2"
        
        response = http_client.post(
            "/v1/execute",
            json={
                "source_code": f"""
import os
# Create file in workspace (this is tracked automatically)
with open('{file_name}', 'w') as f:
    f.write('{content}')
print(f"Created {file_name}")
""",
                "chat_id": chat_id
            }
        )
        
        assert response.status_code == 200
        result = response.json()
        
        file_path = f"/workspace/{file_name}"
        assert file_path in result["files"], f"File {file_path} not found in response: {result}"
        file_hashes[file_name] = result["files"][file_path]
    
    # Update file download limits in database
    response = http_client.post(
        "/v1/execute",
        json={
            "source_code": f"""
import sqlite3
# Access database directly
conn = sqlite3.connect('/storage/file_mgmt_db.sqlite3')
# Set limits: 2 downloads for file1, 1 for file2, unlimited for file3
conn.execute("UPDATE files SET remaining = 2 WHERE file_hash = '{file_hashes['test_file1']}'")
conn.execute("UPDATE files SET remaining = 1 WHERE file_hash = '{file_hashes['test_file2']}'")
conn.execute("UPDATE files SET remaining = NULL WHERE file_hash = '{file_hashes['test_file3']}'")
conn.commit()
print("Updated download limits")
print(f"File hashes: {file_hashes}")
conn.close()
""",
            "chat_id": "test_chat_1"
        }
    )
    
    assert response.status_code == 200
    print(f"Setup complete: {file_hashes}")
    
    return file_hashes, test_files

def test_successful_download(http_client, setup_test_files):
    file_hashes, test_files = setup_test_files
    
    response = http_client.post(
        "/v1/download", 
        json={
            "chat_id": "test_chat_1",
            "file_hash": file_hashes["test_file1"],
            "filename": "myfile.txt"
        }
    )
    
    assert response.status_code == 200
    assert "text/plain" in response.headers["Content-Type"]
    assert "attachment; filename=myfile.txt" in response.headers["Content-Disposition"]
    assert response.content.decode() == test_files["test_file1"]

def test_successful_download(http_client, setup_test_files):
    file_hashes, files_data = setup_test_files
    
    # Request a file download
    response = http_client.post(
        "/v1/download", 
        json={
            "chat_id": "test_chat_1",
            "file_hash": file_hashes["test_file1"],
            "filename": "myfile.txt"
        }
    )
    
    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("text/plain")
    assert "attachment; filename=myfile.txt" in response.headers["Content-Disposition"]
    assert response.content.decode() == files_data["test_file1"]

def test_wrong_chat_id(http_client, setup_test_files):
    file_hashes, files_data = setup_test_files
    
    # Try with wrong chat ID
    response = http_client.post(
        "/v1/download", 
        json={
            "chat_id": "wrong_chat_id",
            "file_hash": file_hashes["test_file1"],
            "filename": "myfile.txt"
        }
    )
    
    assert response.status_code == 404
    # Note: The API returns 404 for both not found and unauthorized access to prevent enumeration

def test_file_not_found(http_client, setup_test_files):
    file_hashes, files_data = setup_test_files
    
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
    file_hashes, files_data = setup_test_files
    
    # First download should succeed
    response1 = http_client.post(
        "/v1/download", 
        json={
            "chat_id": "test_chat_1",
            "file_hash": file_hashes["test_file2"],
            "filename": "myfile.txt"
        }
    )
    assert response1.status_code == 200
    
    # Second download should fail (limit was 1)
    response2 = http_client.post(
        "/v1/download", 
        json={
            "chat_id": "test_chat_1",
            "file_hash": file_hashes["test_file2"],
            "filename": "myfile.txt"
        }
    )
    assert response2.status_code == 404
    # API returns 404 for both not found and permission errors

def test_unlimited_downloads(http_client, setup_test_files):
    file_hashes, files_data = setup_test_files
    
    # Do multiple downloads of the unlimited file
    for _ in range(3):
        response = http_client.post(
            "/v1/download", 
            json={
                "chat_id": "test_chat_2",
                "file_hash": file_hashes["test_file3"],
                "filename": "myfile.txt"
            }
        )
        assert response.status_code == 200
        assert response.content.decode() == files_data["test_file3"]

def test_content_type_detection(http_client, setup_test_files):
    file_hashes, files_data = setup_test_files
    
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
                "file_hash": file_hashes["test_file1"],
                "filename": filename
            }
        )
        
        assert response.status_code == 200
        assert response.headers["Content-Type"].startswith(expected_type)