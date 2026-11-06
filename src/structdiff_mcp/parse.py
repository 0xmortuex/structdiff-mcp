"""Parse JSON / JSONC / YAML / TOML / .env / INI / CSV text into plain Python data.

Everything comes out as dict[str, ...] / list / str / int / float / bool / None, so the
diff engine only deals with JSON-shaped values. YAML is loaded with a SafeLoader
subclass only: python-specific tags (!!python/object, ...) are rejected.
"""

from __future__ import annotations

import base64
import configparser
import datetime as dt
import json
import math
import os
import sys
from dataclasses import dataclass, field
from typing import Any

import yaml

from . import csvio
from .errors import StructDiffError

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on 3.10 CI only
    import tomli as tomllib

FORMATS = ("json", "jsonc", "yaml", "toml", "env", "ini", "csv")

_EXTENSIONS = {
    ".json": "json",
    ".jsonc": "jsonc",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".env": "env",
    ".ini": "ini",
    ".cfg": "ini",
    ".csv": "csv",
    ".tsv": "csv",
}

MAX_DEPTH = 100
MAX_NODES = 5_000_000


@dataclass
class Parsed:
    data: Any
    warnings: list[str] = field(default_factory=list)


def detect_format(path: str) -> str:
    base = os.path.basename(path).lower()
    if base == ".env" or base.startswith(".env."):
        return "env"
    ext = os.path.splitext(base)[1]
    fmt = _EXTENSIONS.get(ext)
    if fmt is None:
        raise StructDiffError(
            f"cannot tell the format of {os.path.basename(path)!r} from its name; "
            f"pass format= one of {', '.join(FORMATS)}"
        )
    return fmt


# ---------------------------------------------------------------- JSONC


def strip_jsonc(text: str) -> str:
    """Remove // and /* */ comments and trailing commas, keeping line/column positions
    (comment characters become spaces; newlines are kept)."""
    out: list[str] = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
        elif text.startswith("//", i):
            while i < n and text[i] not in "\r\n":
                out.append(" ")
                i += 1
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            if j < 0:
                raise StructDiffError("unterminated /* comment in JSONC")
            out.extend(ch if ch in "\r\n" else " " for ch in text[i : j + 2])
            i = j + 2
        else:
            out.append(c)
            i += 1
    return _strip_trailing_commas("".join(out))


def _strip_trailing_commas(text: str) -> str:
    out = list(text)
    in_str = False
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == ",":
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j < n and text[j] in "]}":
                out[i] = " "
        i += 1
    return "".join(out)


# ---------------------------------------------------------------- loaders


def _load_json(text: str, warnings: list[str]) -> Any:
    dups: set[str] = set()

    def hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        d: dict[str, Any] = {}
        for k, v in pairs:
            if k in d:
                dups.add(k)
            d[k] = v
        return d

    try:
        data = json.loads(text, object_pairs_hook=hook)
    except json.JSONDecodeError as e:
        hint = ""
        if "//" in text or "/*" in text:
            hint = " (the file has comments or trailing commas: pass strip_comments=true or use .jsonc)"
        raise StructDiffError(
            f"invalid JSON: {e.msg} at line {e.lineno} column {e.colno}{hint}"
        ) from None
    except RecursionError:
        raise StructDiffError(f"JSON nesting is deeper than {MAX_DEPTH} levels") from None
    except ValueError as e:  # e.g. int with more than 4300 digits
        raise StructDiffError(f"invalid JSON number: {e}") from None
    if dups:
        warnings.append(
            f"duplicate object key(s) {sorted(dups)} in JSON: the last value wins, earlier ones are ignored"
        )
    return data


class _Loader(yaml.SafeLoader):
    """SafeLoader that also records duplicate mapping keys."""

    def __init__(self, stream: str) -> None:
        super().__init__(stream)
        self.dups: set[str] = set()

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        seen: set[Any] = set()
        for key_node, _ in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge" or not isinstance(key_node, yaml.ScalarNode):
                continue
            key = self.construct_object(key_node, deep=True)
            try:
                if key in seen:
                    self.dups.add(str(key))
                seen.add(key)
            except TypeError:
                continue
        return super().construct_mapping(node, deep)


def _load_yaml(text: str, warnings: list[str]) -> Any:
    loader = _Loader(text)
    docs: list[Any] = []
    try:
        while loader.check_data():  # type: ignore[no-untyped-call]
            docs.append(loader.get_data())
    except yaml.YAMLError as e:
        raise StructDiffError(f"invalid or unsafe YAML: {e}") from None
    except RecursionError:
        raise StructDiffError(f"YAML nesting is deeper than {MAX_DEPTH} levels") from None
    finally:
        loader.dispose()
    if loader.dups:
        warnings.append(
            f"duplicate mapping key(s) {sorted(loader.dups)} in YAML: the last value wins"
        )
    if not docs:
        return None
    if len(docs) == 1:
        return docs[0]
    warnings.append(
        f"multi-document YAML ({len(docs)} documents): compared as a list, paths start with $[0], $[1], ..."
    )
    return docs


def _load_toml(text: str) -> Any:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise StructDiffError(f"invalid TOML: {e}") from None


def _load_env(text: str, warnings: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    dups: set[str] = set()
    for n, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise StructDiffError(f".env line {n} has no '=': {raw!r}")
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not key:
            raise StructDiffError(f".env line {n} has an empty variable name")
        if value[:1] == '"':
            end = _closing_quote(value)
            if end < 0:
                raise StructDiffError(f".env line {n}: unterminated double quote")
            body = value[1:end]
            value = (
                body.replace("\\n", "\n").replace("\\t", "\t").replace('\\"', '"').replace("\\\\", "\\")
            )
        elif value[:1] == "'":
            end = value.find("'", 1)
            if end < 0:
                raise StructDiffError(f".env line {n}: unterminated single quote")
            value = value[1:end]
        else:
            cut = value.find(" #")
            if cut >= 0:
                value = value[:cut].rstrip()
        if key in out:
            dups.add(key)
        out[key] = value
    if dups:
        warnings.append(f"duplicate .env variable(s) {sorted(dups)}: the last value wins")
    return out


def _closing_quote(value: str) -> int:
    i = 1
    while i < len(value):
        if value[i] == "\\":
            i += 2
            continue
        if value[i] == '"':
            return i
        i += 1
    return -1


def _load_ini(text: str) -> dict[str, Any]:
    # A default_section name that never occurs makes [DEFAULT] an ordinary section,
    # so inherited values are not copied into every other section.
    cp = configparser.ConfigParser(
        interpolation=None, allow_no_value=True, default_section="\x00nodefault"
    )
    cp.optionxform = str  # type: ignore[assignment,method-assign]  # keep key case
    try:
        cp.read_string(text)
    except configparser.Error as e:
        raise StructDiffError(f"invalid INI: {e}") from None
    return {s: dict(cp.items(s)) for s in cp.sections()}


# ---------------------------------------------------------------- normalisation


class _Norm:
    def __init__(self) -> None:
        self.nodes = 0
        self.stack: set[int] = set()

    def run(self, v: Any, depth: int = 0) -> Any:
        self.nodes += 1
        if self.nodes > MAX_NODES:
            raise StructDiffError(
                f"document expands to more than {MAX_NODES} values (YAML alias bomb or huge file)"
            )
        if depth > MAX_DEPTH:
            raise StructDiffError(f"nesting is deeper than {MAX_DEPTH} levels")
        if v is None or isinstance(v, (bool, str, int, float)):
            return v
        if isinstance(v, (dt.datetime, dt.date, dt.time)):
            return v.isoformat()
        if isinstance(v, (bytes, bytearray)):
            return base64.b64encode(bytes(v)).decode("ascii")
        if isinstance(v, (dict, list, tuple, set, frozenset)):
            if id(v) in self.stack:
                raise StructDiffError("recursive YAML anchor/alias (a value contains itself) is not supported")
            self.stack.add(id(v))
            try:
                if isinstance(v, dict):
                    d: dict[str, Any] = {}
                    for k, item in v.items():
                        ks = self._key(k)
                        if ks in d:
                            raise StructDiffError(
                                f"two different keys both become {ks!r} when converted to text"
                            )
                        d[ks] = self.run(item, depth + 1)
                    return d
                items = list(v)
                if isinstance(v, (set, frozenset)):
                    items.sort(key=repr)
                return [self.run(x, depth + 1) for x in items]
            finally:
                self.stack.discard(id(v))
        raise StructDiffError(f"unsupported value type {type(v).__name__}")

    @staticmethod
    def _key(k: Any) -> str:
        if isinstance(k, str):
            return k
        if k is None or isinstance(k, (bool, int, float)):
            return json.dumps(k)
        if isinstance(k, (dt.datetime, dt.date, dt.time)):
            return k.isoformat()
        return str(k)


def normalize(data: Any) -> Any:
    return _Norm().run(data)


def has_nonfinite(v: Any) -> bool:
    if isinstance(v, float):
        return not math.isfinite(v)
    if isinstance(v, dict):
        return any(has_nonfinite(x) for x in v.values())
    if isinstance(v, list):
        return any(has_nonfinite(x) for x in v)
    return False


# ---------------------------------------------------------------- entry point


def parse_text(text: str, fmt: str, strip_comments: bool = False) -> Parsed:
    if fmt not in FORMATS:
        raise StructDiffError(f"unknown format {fmt!r}; use one of {', '.join(FORMATS)}")
    if text.startswith("﻿"):
        text = text[1:]
    warnings: list[str] = []
    if fmt in ("json", "jsonc"):
        if strip_comments or fmt == "jsonc":
            text = strip_jsonc(text)
        data = _load_json(text, warnings)
    elif fmt == "yaml":
        data = _load_yaml(text, warnings)
    elif fmt == "toml":
        data = _load_toml(text)
    elif fmt == "env":
        data = _load_env(text, warnings)
    elif fmt == "ini":
        data = _load_ini(text)
    else:
        data = csvio.parse_csv_text(text)
    return Parsed(normalize(data), warnings)
