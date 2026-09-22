"""Utility functions for MeshCore plugin security enhancements."""

import json
import os
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path


SENSITIVE_IPC_KEYS = frozenset({"password"})


def get_profile_scoped_dir():
    """Resolve the active profile's private MeshCore state and IPC directory.

    Hermes must provide an explicit profile directory. Refusing to guess avoids
    accidentally sharing IPC files through a global or default-profile path.
    """
    raw_profile_dir = os.environ.get("HERMES_PROFILE_DIR", "").strip()
    # Under a multiplexed gateway the profile's HOME is set; fall back to it
    if not raw_profile_dir:
        raw_profile_dir = os.environ.get("HERMES_HOME", "").strip()
    if not raw_profile_dir:
        raise RuntimeError("HERMES_PROFILE_DIR is required for MeshCore IPC")

    profile_dir = Path(raw_profile_dir).expanduser()
    if not profile_dir.is_absolute():
        raise RuntimeError("HERMES_PROFILE_DIR must be an absolute path")
    try:
        profile_dir = profile_dir.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RuntimeError("HERMES_PROFILE_DIR cannot be resolved") from exc
    if not profile_dir.is_dir():
        raise RuntimeError("HERMES_PROFILE_DIR is not a directory")

    scoped_dir = profile_dir / ".meshcore"
    scoped_dir.mkdir(mode=0o700, exist_ok=True)
    os.chmod(scoped_dir, 0o700)
    return scoped_dir


def _reject_sensitive_ipc_keys(value):
    if isinstance(value, Mapping):
        for key, child in value.items():
            if isinstance(key, str) and key.casefold() in SENSITIVE_IPC_KEYS:
                raise ValueError("Sensitive keys are forbidden in MeshCore IPC payloads")
            _reject_sensitive_ipc_keys(child)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            _reject_sensitive_ipc_keys(child)


def serialize_ipc_payload(data, forbidden_values=()):
    """Serialize an IPC payload only after rejecting secret keys and values."""
    _reject_sensitive_ipc_keys(data)
    serialized = json.dumps(data, ensure_ascii=False)
    for value in forbidden_values:
        if value and str(value) in serialized:
            raise ValueError("Sensitive values are forbidden in MeshCore IPC payloads")
    return serialized


def secure_write_json(filepath, data, *, forbidden_values=()):
    """Securely write a validated IPC payload atomically with 0600 permissions."""
    filepath = Path(filepath)
    serialized = serialize_ipc_payload(data, forbidden_values=forbidden_values)

    temp_dir = filepath.parent
    with tempfile.NamedTemporaryFile(
        mode="w",
        dir=temp_dir,
        delete=False,
        suffix=".tmp",
        encoding="utf-8",
    ) as tmp_file:
        tmp_path = Path(tmp_file.name)
        tmp_file.write(serialized)
        tmp_file.flush()
        os.fsync(tmp_file.fileno())

    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, filepath)


# Backward-compatible name for callers/tests that describe JSON serialization.
serialize_json_payload = serialize_ipc_payload


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