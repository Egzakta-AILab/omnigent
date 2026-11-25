"""Locate citations in the Git patch captured for a review."""

from __future__ import annotations

import hashlib
import re
from typing import Literal

MAX_REVIEW_PATCH_BYTES = 48 * 1024
PATCH_CHUNK_BYTES = 12 * 1024
MAX_PATCH_CHUNKS = 64
MAX_SAVED_REVIEW_PATCH_BYTES = MAX_PATCH_CHUNKS * PATCH_CHUNK_BYTES

Side = Literal["base", "head"]
Anchor = tuple[str, Side, int]

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?$")
_ESCAPE = re.compile(r'\\([0-7]{3}|[abfnrtv"\\])')
_ESCAPE_BYTES = {
    "a": b"\a",
    "b": b"\b",
    "f": b"\f",
    "n": b"\n",
    "r": b"\r",
    "t": b"\t",
    "v": b"\v",
    '"': b'"',
    "\\": b"\\",
}


def patch_digest(patch: str) -> str:
    return hashlib.sha256(patch.encode("utf-8")).hexdigest()


def _path(header: str, prefix: str) -> str | None:
    # Git delimits an unquoted path containing spaces with a tab. Quoted paths
    # use C escapes (including octal UTF-8 bytes), rather than JSON escaping.
    field = header.split("\t", 1)[0]
    if field == "/dev/null":
        return None
    if field.startswith('"'):
        if not field.endswith('"') or len(field) < 2:
            raise ValueError("Unterminated quoted diff path")
        encoded = field[1:-1]
        decoded = bytearray()
        cursor = 0
        for match in _ESCAPE.finditer(encoded):
            literal = encoded[cursor : match.start()]
            if "\\" in literal or '"' in literal:
                raise ValueError("Unsupported quoted diff path")
            decoded.extend(literal.encode("utf-8"))
            escape = match[1]
            decoded.extend(_ESCAPE_BYTES[escape] if len(escape) == 1 else bytes([int(escape, 8)]))
            cursor = match.end()
        literal = encoded[cursor:]
        if "\\" in literal or '"' in literal:
            raise ValueError("Unsupported quoted diff path")
        decoded.extend(literal.encode("utf-8"))
        field = decoded.decode("utf-8")
    if not field.startswith(prefix) or not field[len(prefix) :]:
        raise ValueError("Unexpected diff path prefix")
    return field[len(prefix) :]


def _patch_lines(patch: str, *, include_context: bool) -> dict[Anchor, str]:
    lines: dict[Anchor, str] = {}
    old_path = new_path = None
    saw_file = saw_old = saw_new = False
    old_line = new_line = old_left = new_left = 0
    for raw in patch.split("\n"):
        if raw == "\\ No newline at end of file":
            continue
        if old_left or new_left:
            marker = raw[:1]
            if marker == " " and old_left and new_left:
                if include_context and old_path is not None and new_path is not None:
                    lines[(old_path, "base", old_line)] = raw[1:]
                    lines[(new_path, "head", new_line)] = raw[1:]
                old_line, new_line = old_line + 1, new_line + 1
                old_left, new_left = old_left - 1, new_left - 1
            elif marker == "-" and old_left and old_path is not None:
                lines[(old_path, "base", old_line)] = raw[1:]
                old_line, old_left = old_line + 1, old_left - 1
            elif marker == "+" and new_left and new_path is not None:
                lines[(new_path, "head", new_line)] = raw[1:]
                new_line, new_left = new_line + 1, new_left - 1
            else:
                raise ValueError("Invalid line or count inside diff hunk")
            continue

        header = raw.rstrip("\r")
        if header.startswith("diff --git "):
            saw_file = True
            saw_old = saw_new = False
            old_path = new_path = None
        elif header.startswith("--- ") and saw_file and not saw_old:
            old_path = _path(header[4:], "a/")
            saw_old = True
        elif header.startswith("+++ ") and saw_old and not saw_new:
            new_path = _path(header[4:], "b/")
            saw_new = True
        elif header.startswith("@@"):
            match = _HUNK.fullmatch(header)
            if match is None or not saw_new:
                raise ValueError("Missing text headers or invalid diff hunk")
            old_line, new_line = int(match[1]), int(match[3])
            old_left, new_left = int(match[2] or "1"), int(match[4] or "1")
            if (old_left and (old_line < 1 or old_path is None)) or (
                new_left and (new_line < 1 or new_path is None)
            ):
                raise ValueError("Invalid start or path for diff hunk")
        elif saw_new and header[:1] in (" ", "+", "-"):
            raise ValueError("Unexpected changed line outside a hunk")
    if old_left or new_left:
        raise ValueError("Truncated diff hunk")
    return lines


def changed_lines(patch: str) -> dict[Anchor, str]:
    """Return exact added and removed rows, rejecting malformed hunks."""
    return _patch_lines(patch, include_context=False)


def changed_anchors(patch: str) -> frozenset[Anchor]:
    return frozenset(changed_lines(patch))


def displayed_lines(patch: str) -> dict[Anchor, str]:
    """Return changed and context rows present on each side of the saved diff."""
    return _patch_lines(patch, include_context=True)


def displayed_anchors(patch: str) -> frozenset[Anchor]:
    return frozenset(displayed_lines(patch))
