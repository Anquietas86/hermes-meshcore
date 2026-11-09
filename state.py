"""Profile-scoped state and IPC files for the MeshCore plugin.

Everything lives under ``<hermes home>/.meshcore/`` where the hermes home is
resolved with ``hermes_constants.get_hermes_home()``. Under a multiplexed
gateway that call honours the per-profile context override, so two profiles
never share state or IPC files. (Reading ``os.environ["HERMES_HOME"]`` does
not: the multiplexer never writes profile homes into the process env.)

Files are written atomically with mode 0600 inside a 0700 directory. IPC
requests are one file per request, so concurrent callers cannot clobber each
other. Passwords are never written to disk.

This module is stdlib-only and import-light: the dashboard backend loads it
by file path.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

STATE_DIR_NAME = ".meshcore"
STATE_FILE = "state.json"
REQUESTS_DIR = "requests"
RESPONSES_DIR = "responses"
STATE_FRESH_SECONDS = 90
REQUEST_TTL_SECONDS = 180
SENSITIVE_KEYS = frozenset({"password", "secret", "token"})


def hermes_home() -> Path:
    """The active profile's Hermes home (context-aware under multiplexing)."""
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home()).expanduser()
    except Exception:
        raw = os.environ.get("HERMES_HOME", "").strip()
        return Path(raw).expanduser() if raw else Path.home() / ".hermes"


def home_key(home: Optional[Path] = None) -> str:
    """Stable key for a home directory (used to index live adapters)."""
    home = Path(home) if home is not None else hermes_home()
    try:
        return str(home.resolve())
    except OSError:
        return str(home.absolute())


def state_dir(home: Optional[Path] = None, *, create: bool = True) -> Path:
    base = Path(home) if home is not None else hermes_home()
    path = base / STATE_DIR_NAME
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path, 0o700)
        for sub in (REQUESTS_DIR, RESPONSES_DIR):
            (path / sub).mkdir(mode=0o700, exist_ok=True)
    return path


def reject_sensitive(value: Any) -> None:
    """Raise if ``value`` carries a key that looks like a secret."""
    if isinstance(value, Mapping):
        for key, child in value.items():
            if isinstance(key, str) and key.casefold() in SENSITIVE_KEYS:
                raise ValueError(f"refusing to write sensitive key {key!r} to disk")
            reject_sensitive(child)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            reject_sensitive(child)


def write_json(path: Path, data: Any) -> None:
    reject_sensitive(data)
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path: Path) -> Optional[Any]:
    """Read a 0600 JSON file owned by this user; ``None`` if missing or unreadable."""
    path = Path(path)
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    if st.st_mode & 0o077:
        raise PermissionError(f"{path} is readable by other users ({oct(st.st_mode & 0o777)})")
    if hasattr(os, "getuid") and st.st_uid != os.getuid():
        raise PermissionError(f"{path} is not owned by the current user")
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


# ── Gateway state snapshot ───────────────────────────────────────────────

def write_state(snapshot: Dict[str, Any], home: Optional[Path] = None) -> None:
    snapshot = dict(snapshot, updated_at=time.time())
    write_json(state_dir(home) / STATE_FILE, snapshot)


def read_state(home: Optional[Path] = None) -> Dict[str, Any]:
    """The gateway's last snapshot, with ``fresh`` set when it is recent."""
    try:
        data = read_json(state_dir(home, create=False) / STATE_FILE)
    except PermissionError as exc:
        return {"connected": False, "fresh": False, "error": str(exc)}
    if not isinstance(data, dict):
        return {"connected": False, "fresh": False, "error": "MeshCore gateway is not running (no state file)"}
    age = time.time() - float(data.get("updated_at") or 0)
    data["age_s"] = round(age, 1)
    data["fresh"] = age <= STATE_FRESH_SECONDS
    if not data["fresh"]:
        data["connected"] = False
    return data


# ── Request / response IPC ───────────────────────────────────────────────

def new_request_id() -> str:
    return f"{int(time.time())}-{secrets.token_hex(6)}"


def _valid_id(request_id: str) -> bool:
    return bool(request_id) and all(c.isalnum() or c == "-" for c in request_id) and len(request_id) <= 64


def submit_request(kind: str, payload: Dict[str, Any], home: Optional[Path] = None) -> str:
    """Queue a request for the gateway; returns its id."""
    request_id = new_request_id()
    body = {"id": request_id, "kind": kind, "payload": payload, "submitted_at": time.time()}
    write_json(state_dir(home) / REQUESTS_DIR / f"{request_id}.json", body)
    return request_id


def read_response(request_id: str, home: Optional[Path] = None, *, consume: bool = True) -> Optional[Dict[str, Any]]:
    if not _valid_id(request_id):
        raise ValueError("invalid request id")
    path = state_dir(home) / RESPONSES_DIR / f"{request_id}.json"
    data = read_json(path)
    if data is not None and consume:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    return data


def request_pending(request_id: str, home: Optional[Path] = None) -> bool:
    if not _valid_id(request_id):
        return False
    return (state_dir(home) / REQUESTS_DIR / f"{request_id}.json").exists()


async def call_gateway(kind: str, payload: Dict[str, Any], *, timeout: float,
                       home: Optional[Path] = None, poll: float = 0.5) -> Dict[str, Any]:
    """Submit a request and wait for the gateway's answer."""
    state = read_state(home)
    if not state.get("fresh"):
        return {"success": False, "error": state.get("error") or "MeshCore gateway is not running or its state is stale"}
    request_id = submit_request(kind, payload, home)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await asyncio.sleep(poll)
        result = read_response(request_id, home)
        if result is not None:
            return result
    # Withdraw the request if the gateway never picked it up.
    try:
        (state_dir(home) / REQUESTS_DIR / f"{request_id}.json").unlink()
    except FileNotFoundError:
        pass
    return {"success": False, "error": f"Timed out after {int(timeout)}s waiting for the MeshCore gateway"}


def take_requests(home: Optional[Path] = None) -> Iterator[Tuple[str, Dict[str, Any]]]:
    """Gateway side: yield and remove queued requests, oldest first. Expired ones are dropped."""
    req_dir = state_dir(home) / REQUESTS_DIR
    now = time.time()
    for path in sorted(req_dir.glob("*.json")):
        try:
            data = read_json(path)
        except PermissionError:
            data = None
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        if not isinstance(data, dict) or not _valid_id(str(data.get("id", ""))):
            continue
        if now - float(data.get("submitted_at") or 0) > REQUEST_TTL_SECONDS:
            continue
        yield data["id"], data


def write_response(request_id: str, result: Dict[str, Any], home: Optional[Path] = None) -> None:
    if not _valid_id(request_id):
        raise ValueError("invalid request id")
    write_json(state_dir(home) / RESPONSES_DIR / f"{request_id}.json",
               dict(result, request_id=request_id, completed_at=time.time()))


def prune_responses(home: Optional[Path] = None, max_age: float = REQUEST_TTL_SECONDS) -> None:
    now = time.time()
    for path in (state_dir(home) / RESPONSES_DIR).glob("*.json"):
        try:
            if now - path.stat().st_mtime > max_age:
                path.unlink()
        except OSError:
            pass


def list_contacts(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    contacts = state.get("contacts_list")
    return contacts if isinstance(contacts, list) else []
