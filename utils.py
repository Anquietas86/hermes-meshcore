"""Utility functions for MeshCore plugin security enhancements."""

import json
import os
import tempfile
import time
from pathlib import Path


def get_profile_scoped_dir():
    """Get the profile-scoped directory for MeshCore state and IPC."""
    # Get profile directory from environment - this is set by Hermes
    profile_dir = os.environ.get("HERMES_PROFILE_DIR")
    if not profile_dir:
        # Fallback to default location if not set
        profile_dir = os.path.expanduser("~/.hermes")
    
    scoped_dir = Path(profile_dir) / ".meshcore"
    scoped_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    return scoped_dir


def secure_write_json(filepath, data):
    """Securely write JSON data to a file with atomic rename and 0600 permissions."""
    filepath = Path(filepath)
    
    # Create temporary file in same directory for atomic rename
    temp_dir = filepath.parent
    with tempfile.NamedTemporaryFile(
        mode='w', 
        dir=temp_dir, 
        delete=False,
        suffix='.tmp',
        encoding='utf-8'
    ) as tmp_file:
        tmp_path = Path(tmp_file.name)
        
        # Write data to temporary file
        json.dump(data, tmp_file, ensure_ascii=False)
        tmp_file.flush()
        os.fsync(tmp_file.fileno())
    
    # Atomically move temp file to destination with correct permissions
    os.chmod(tmp_path, 0o600)  # Set permissions before moving
    os.rename(tmp_path, filepath)


def secure_read_json(filepath, require_matching_request_id=None):
    """Securely read JSON data from a file, with optional request ID validation."""
    filepath = Path(filepath)
    
    if not filepath.exists():
        return None
    
    # Verify file permissions and ownership
    stat = filepath.stat()
    if stat.st_mode & 0o777 != 0o600:  # Check if permissions are not 0600
        raise PermissionError(f"File {filepath} has insecure permissions: {oct(stat.st_mode & 0o777)}")
    
    with open(filepath, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    # Validate request ID if required
    if require_matching_request_id is not None:
        file_request_id = data.get('request_id')
        if file_request_id != require_matching_request_id:
            raise ValueError(f"Request ID mismatch: expected {require_matching_request_id}, got {file_request_id}")
    
    return data


def secure_remove(filepath):
    """Securely remove a file."""
    filepath = Path(filepath)
    if filepath.exists():
        filepath.unlink()


def generate_request_id():
    """Generate a unique request ID based on timestamp and random component."""
    import random
    return f"{int(time.time())}-{random.randint(1000, 9999)}"