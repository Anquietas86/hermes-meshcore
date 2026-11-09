"""Text helpers: sanitising, byte-safe splitting, mentions, sender parsing."""

from __future__ import annotations

import re
from typing import Iterable, List, Optional, Tuple

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f�]")
_MARKDOWN = (
    (re.compile(r"\*\*(.+?)\*\*", re.S), r"\1"),
    (re.compile(r"__(.+?)__", re.S), r"\1"),
    (re.compile(r"`{1,3}([^`]*)`{1,3}"), r"\1"),
    (re.compile(r"^#{1,6}\s+", re.M), ""),
    (re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)"), r"\1 \2"),
)


def sanitize(text: str) -> str:
    """Drop NULs and control characters firmware may treat as terminators."""
    return _CONTROL.sub("", text or "")


def plain_text(text: str) -> str:
    """Strip the markdown a model tends to emit even when told not to."""
    for pattern, repl in _MARKDOWN:
        text = pattern.sub(repl, text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _byte_prefix(text: str, limit: int) -> str:
    """Longest prefix of ``text`` whose UTF-8 encoding fits in ``limit`` bytes."""
    raw = text.encode("utf-8")[:limit]
    return raw.decode("utf-8", "ignore")


def split_bytes(text: str, limit: int) -> List[str]:
    """Split into chunks of at most ``limit`` UTF-8 bytes, preferring whitespace."""
    if limit < 8:
        raise ValueError("limit too small")
    chunks: List[str] = []
    rest = text.strip()
    while rest:
        if len(rest.encode("utf-8")) <= limit:
            chunks.append(rest)
            break
        head = _byte_prefix(rest, limit)
        cut = max(head.rfind(" "), head.rfind("\n"))
        if cut >= len(head) // 3:
            head = head[:cut]
        chunk = head.strip()
        if not chunk:  # a single unbreakable run; take it whole
            chunk = head
        chunks.append(chunk)
        rest = rest[len(head):].strip()
    return chunks or [""]


def split_for_mesh(text: str, limit: int, max_chunks: int = 0) -> List[str]:
    """Split ``text`` into mesh packets with ``(i/n)`` markers when it needs more than one.

    ``max_chunks`` > 0 caps the number of packets; the last one is cut with an
    ellipsis rather than flooding the mesh with a wall of text.
    """
    text = sanitize(text).strip()
    if len(text.encode("utf-8")) <= limit:
        return [text]
    marker_room = len(" (99/99)")
    body = split_bytes(text, limit - marker_room)
    if max_chunks and len(body) > max_chunks:
        body = body[:max_chunks]
        body[-1] = _byte_prefix(body[-1], limit - marker_room - 3).rstrip() + "..."
    total = len(body)
    return [f"{chunk} ({i}/{total})" for i, chunk in enumerate(body, 1)]


def split_sender(text: str) -> Tuple[str, str]:
    """Channel texts arrive as ``"<sender name>: <message>"``."""
    name, sep, message = text.partition(": ")
    if not sep or not name or len(name) > 40:
        return "", text.strip()
    return name.strip(), message.strip()


def strip_mention(text: str, names: Iterable[str]) -> Optional[str]:
    """Return ``text`` with a leading mention of one of ``names`` removed, or None if not addressed.

    Accepts the MeshCore app's ``@[Full Node Name]`` anywhere in the message,
    and ``@name`` / ``name:`` / ``name,`` at the start.
    """
    names = [n for n in (str(x).strip() for x in names) if n]
    folded = text.casefold()
    for name in names:
        tag = f"@[{name}]".casefold()
        at = folded.find(tag)
        if at >= 0:
            return re.sub(r"\s{2,}", " ", text[:at] + text[at + len(tag):]).strip(" ,:")
    for name in names:
        escaped = re.escape(name)
        for pattern in (r"\s*@" + escaped + r"(?=[\s,:!?]|$)[\s,:]*",   # "@bot hello"
                        r"\s*" + escaped + r"\s*[:,]\s*"):              # "bot: hello"
            match = re.match(pattern, text, re.I)
            if match:
                return text[match.end():].strip()
    return None


def parse_int_set(raw) -> set:
    out = set()
    for part in _as_list(raw):
        part = str(part).strip()
        if part.lstrip("-").isdigit():
            out.add(int(part))
    return out


def _as_list(raw) -> List[str]:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        return [str(x).strip() for x in raw if str(x).strip()]
    if isinstance(raw, bool):
        return [str(raw).lower()]
    return [x.strip() for x in str(raw).replace(";", ",").split(",") if x.strip()]


as_list = _as_list


def as_bool(raw, default: bool) -> bool:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return default
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


HEX = re.compile(r"^[0-9a-f]+$")


def normalize_pubkey(entry: str) -> str:
    """Lower-case hex pubkey (prefix); empty string if not hex."""
    entry = str(entry or "").strip().lower()
    if entry.startswith("dm:"):
        entry = entry[3:]
    return entry if HEX.match(entry) else ""
