"""RFC 6902 JSON Patch application (add, remove, replace, move, copy, test)."""

from __future__ import annotations

import copy
from typing import Any

from .diff import strict_equal
from .errors import StructDiffError


def parse_pointer(ptr: Any) -> list[str]:
    if not isinstance(ptr, str):
        raise StructDiffError(f"JSON Pointer must be a string, got {ptr!r}")
    if ptr == "":
        return []
    if not ptr.startswith("/"):
        raise StructDiffError(f"JSON Pointer {ptr!r} must be empty or start with '/'")
    return [t.replace("~1", "/").replace("~0", "~") for t in ptr[1:].split("/")]


def _index(token: str, length: int, allow_end: bool) -> int:
    if allow_end and token == "-":
        return length
    if not token.isdigit() or (len(token) > 1 and token[0] == "0"):
        raise StructDiffError(f"{token!r} is not a valid array index")
    i = int(token)
    if i > length or (i == length and not allow_end):
        raise StructDiffError(f"array index {i} is out of range (length {length})")
    return i


def _walk(doc: Any, tokens: list[str]) -> Any:
    cur = doc
    for t in tokens:
        if isinstance(cur, dict):
            if t not in cur:
                raise StructDiffError(f"key {t!r} does not exist")
            cur = cur[t]
        elif isinstance(cur, list):
            cur = cur[_index(t, len(cur), False)]
        else:
            raise StructDiffError(f"cannot step into a {type(cur).__name__} with {t!r}")
    return cur


def _get(doc: Any, ptr: str) -> Any:
    return _walk(doc, parse_pointer(ptr))


def _add(doc: Any, ptr: str, value: Any) -> Any:
    tokens = parse_pointer(ptr)
    if not tokens:
        return value
    parent = _walk(doc, tokens[:-1])
    last = tokens[-1]
    if isinstance(parent, dict):
        parent[last] = value
    elif isinstance(parent, list):
        parent.insert(_index(last, len(parent), True), value)
    else:
        raise StructDiffError(f"cannot add into a {type(parent).__name__}")
    return doc


def _remove(doc: Any, ptr: str) -> tuple[Any, Any]:
    tokens = parse_pointer(ptr)
    if not tokens:
        raise StructDiffError("cannot remove the document root")
    parent = _walk(doc, tokens[:-1])
    last = tokens[-1]
    if isinstance(parent, dict):
        if last not in parent:
            raise StructDiffError(f"key {last!r} does not exist")
        return doc, parent.pop(last)
    if isinstance(parent, list):
        return doc, parent.pop(_index(last, len(parent), False))
    raise StructDiffError(f"cannot remove from a {type(parent).__name__}")


def apply_patch(doc: Any, patch: list[dict[str, Any]]) -> Any:
    """Return a new document with the patch applied; the input is not modified."""
    if not isinstance(patch, list):
        raise StructDiffError("a JSON Patch must be a JSON array of operation objects")
    out = copy.deepcopy(doc)
    for n, op in enumerate(patch):
        try:
            out = _apply_one(out, op)
        except StructDiffError as e:
            name = op.get("op") if isinstance(op, dict) else op
            raise StructDiffError(f"patch operation #{n} ({name}) failed: {e}") from None
    return out


def _apply_one(doc: Any, op: Any) -> Any:
    if not isinstance(op, dict) or "op" not in op or "path" not in op:
        raise StructDiffError("each operation must be an object with 'op' and 'path'")
    name = op["op"]
    path = op["path"]
    if name in ("add", "replace", "test") and "value" not in op:
        raise StructDiffError("missing 'value'")
    if name in ("move", "copy") and "from" not in op:
        raise StructDiffError("missing 'from'")
    if name == "add":
        return _add(doc, path, copy.deepcopy(op["value"]))
    if name == "remove":
        return _remove(doc, path)[0]
    if name == "replace":
        _get(doc, path)
        if not parse_pointer(path):
            return copy.deepcopy(op["value"])
        doc, _ = _set_existing(doc, path, copy.deepcopy(op["value"]))
        return doc
    if name == "move":
        src = op["from"]
        s_tok, d_tok = parse_pointer(src), parse_pointer(path)
        if d_tok[: len(s_tok)] == s_tok and len(d_tok) > len(s_tok):
            raise StructDiffError("cannot move a value into itself")
        if s_tok == d_tok:
            _get(doc, src)
            return doc
        doc, value = _remove(doc, src)
        return _add(doc, path, value)
    if name == "copy":
        return _add(doc, path, copy.deepcopy(_get(doc, op["from"])))
    if name == "test":
        if not strict_equal(_get(doc, path), op["value"]):
            raise StructDiffError(f"test failed at {path!r}")
        return doc
    raise StructDiffError(f"unknown op {name!r}; use add, remove, replace, move, copy or test")


def _set_existing(doc: Any, ptr: str, value: Any) -> tuple[Any, None]:
    tokens = parse_pointer(ptr)
    parent = _walk(doc, tokens[:-1])
    last = tokens[-1]
    if isinstance(parent, dict):
        parent[last] = value
    else:
        parent[_index(last, len(parent), False)] = value
    return doc, None
