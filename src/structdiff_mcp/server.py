"""structdiff-mcp: an MCP server that reports what actually changed between two
structured documents (JSON, YAML, TOML, CSV, .env, INI), by path."""

from __future__ import annotations

import argparse
import functools
import json
import os
from collections.abc import Callable
from typing import Any, Literal, TypeVar

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import csvdiff, csvio, parse, sandbox
from .diff import DEFAULT_ARRAY_KEYS, Differ, Options, Report, kind, strict_equal
from .errors import StructDiffError
from .patch import apply_patch

mcp = MCPServer(
    "structdiff",
    instructions=(
        "Semantic diffs of structured files. Unlike a line diff, these ignore key order and "
        "formatting and report what changed by path. diff_files / diff_text compare two JSON, JSONC, "
        "YAML, TOML, .env or INI documents (formats may differ between a and b). Lists of objects are "
        "matched by an id/name/key field when possible (array_match='auto'), so reordering shows as "
        "'moved' instead of dozens of edits. csv_diff compares CSV files row by row using key_columns. "
        "as_json_patch returns an RFC 6902 patch that turns a into b; apply_preview shows the result of "
        "a patch. inspect_file checks how a file parses (format, warnings) before diffing. All file "
        "access is read-only and limited to the directories the server was started with."
    ),
)

F = TypeVar("F", bound=Callable[..., Any])

_EXPECTED = (StructDiffError, FileNotFoundError, IsADirectoryError, PermissionError, json.JSONDecodeError)

MAX_TEXT_CHARS = 10 * 1024 * 1024

Inputs = Literal["files", "text"]
Fmt = Literal["auto", "json", "jsonc", "yaml", "toml", "env", "ini", "csv"]
ArrayMatch = Literal["auto", "index", "key", "set"]


def tool(fn: F) -> F:
    """Register fn as an MCP tool, forwarding expected failures as ToolError."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except _EXPECTED as e:
            raise ToolError(str(e)) from e

    mcp.tool()(wrapper)
    return fn


# ---------------------------------------------------------------- loading


def _load_file(path: str, fmt: str, strip_comments: bool, encoding: str, label: str) -> tuple[parse.Parsed, str]:
    real = sandbox.resolve(path)
    size = os.path.getsize(real)
    if size > sandbox.MAX_STRUCTURED_BYTES:
        raise StructDiffError(
            f"{label} is {size} bytes; the limit for structured documents is {sandbox.MAX_STRUCTURED_BYTES}"
            + ("; use csv_diff, which streams" if real.lower().endswith((".csv", ".tsv")) else "")
        )
    used = parse.detect_format(real) if fmt == "auto" else fmt
    with open(real, "rb") as f:
        raw = f.read()
    text, _ = csvio.decode_bytes(raw, encoding)
    try:
        return parse.parse_text(text, used, strip_comments), used
    except StructDiffError as e:
        raise StructDiffError(f"{label} ({os.path.basename(real)}, parsed as {used}): {e}") from None


def _load_text(text: str, fmt: str, strip_comments: bool, label: str) -> tuple[parse.Parsed, str]:
    if fmt == "auto":
        raise StructDiffError("format is required for pasted text: json, jsonc, yaml, toml, env, ini or csv")
    if len(text) > MAX_TEXT_CHARS:
        raise StructDiffError(f"{label} is {len(text)} characters; the limit for pasted text is {MAX_TEXT_CHARS}")
    try:
        return parse.parse_text(text, fmt, strip_comments), fmt
    except StructDiffError as e:
        raise StructDiffError(f"{label} (parsed as {fmt}): {e}") from None


def _load_pair(
    a: str, b: str, inputs: str, fmt: str, strip_comments: bool, encoding: str
) -> tuple[parse.Parsed, parse.Parsed, list[str]]:
    if inputs == "files":
        pa, fa = _load_file(a, fmt, strip_comments, encoding, "a")
        pb, fb = _load_file(b, fmt, strip_comments, encoding, "b")
    else:
        pa, fa = _load_text(a, fmt, strip_comments, "a")
        pb, fb = _load_text(b, fmt, strip_comments, "b")
    return pa, pb, [fa, fb]


def _options(array_match: str, array_keys: list[str] | None, ignore: list[str] | None, abs_tol: float, rel_tol: float) -> Options:
    return Options(
        array_match=array_match,
        array_keys=tuple(array_keys) if array_keys else DEFAULT_ARRAY_KEYS,
        ignore=tuple(ignore or ()),
        abs_tol=abs_tol,
        rel_tol=rel_tol,
    )


def _check_caps(max_changes: int, max_value_chars: int) -> None:
    if max_changes < 1 or max_value_chars < 10:
        raise StructDiffError("max_changes must be >= 1 and max_value_chars >= 10")


def _run_diff(
    a: str, b: str, inputs: str, fmt: str, array_match: str, array_keys: list[str] | None,
    ignore: list[str] | None, abs_tol: float, rel_tol: float, strip_comments: bool, encoding: str,
    max_changes: int, max_value_chars: int,
) -> dict[str, Any]:
    _check_caps(max_changes, max_value_chars)
    opts = _options(array_match, array_keys, ignore, abs_tol, rel_tol)
    pa, pb, fmts = _load_pair(a, b, inputs, fmt, strip_comments, encoding)
    differ = Differ(opts)
    changes = differ.diff(pa.data, pb.data)
    warnings = [f"a: {w}" for w in pa.warnings] + [f"b: {w}" for w in pb.warnings] + differ.warnings
    out = Report(changes, warnings).to_dict(max_changes, max_value_chars)
    return {"format_a": fmts[0], "format_b": fmts[1], **out}


# ---------------------------------------------------------------- tools


@tool
def diff_files(
    a: str,
    b: str,
    format: Fmt = "auto",
    array_match: ArrayMatch = "auto",
    array_keys: list[str] | None = None,
    ignore: list[str] | None = None,
    abs_tol: float = 0.0,
    rel_tol: float = 0.0,
    strip_comments: bool = False,
    encoding: str = "auto",
    max_changes: int = 200,
    max_value_chars: int = 200,
) -> dict[str, Any]:
    """Semantic diff of two files: what changed, by path, ignoring key order and formatting.

    Reads JSON, JSONC, YAML (safe subset, multi-document supported), TOML, .env and INI files
    (format is detected from the file name; a and b may be different formats). Returns
    {identical, summary{added, removed, changed, type_changed, moved, total}, changes[], warnings[]}.
    Each change has an op ('added', 'removed', 'changed', 'type_changed', 'moved'), a JSONPath-like
    path such as $.servers[id=3].port, and old/new values (truncated to max_value_chars).
    A value going from 1 to "1" is type_changed; null versus a missing key is reported as a key
    that was added or removed, never as a change.

    Args:
        a, b: file paths (relative paths resolve against the first --dir).
        format: json, jsonc, yaml, toml, env, ini, csv, or auto (from the extension).
        array_match: how list items are paired. 'auto' (default) matches lists of objects by the
            first of array_keys that is present and unique in every item, and falls back to
            position; 'index' pairs by position; 'key' is like auto but warns when no key works;
            'set' ignores order and duplicates-aware compares items by value (good for tag lists).
            Items matched by key that changed position are reported as 'moved'.
        array_keys: candidate key fields, default ["id", "name", "key"].
        ignore: patterns of paths to skip. '*' matches any text, including dots and brackets:
            '$.metadata.updatedAt', '$.items[*].etag', '*.timestamp'. Must start with '$' or '*'.
        abs_tol, rel_tol: numbers within these tolerances are treated as equal (0.1+0.2 vs 0.3
            needs abs_tol=1e-9). 1 and 1.0 are always equal; big integers stay exact.
        strip_comments: remove // and /* */ comments and trailing commas before parsing JSON
            (always on for .jsonc).
        encoding: 'auto' honours UTF-8/16/32 BOMs, otherwise UTF-8; or give e.g. 'cp1252'.
        max_changes: cap on listed changes; summary counts are always complete.
    """
    return _run_diff(a, b, "files", format, array_match, array_keys, ignore, abs_tol, rel_tol,
                     strip_comments, encoding, max_changes, max_value_chars)


@tool
def diff_text(
    a: str,
    b: str,
    format: Literal["json", "jsonc", "yaml", "toml", "env", "ini", "csv"],
    array_match: ArrayMatch = "auto",
    array_keys: list[str] | None = None,
    ignore: list[str] | None = None,
    abs_tol: float = 0.0,
    rel_tol: float = 0.0,
    strip_comments: bool = False,
    max_changes: int = 200,
    max_value_chars: int = 200,
) -> dict[str, Any]:
    """Semantic diff of two pasted strings (same output and options as diff_files).

    Use this for content that is not on disk. 'format' is required because it cannot be guessed
    from a name. Both strings must be in the same format. See diff_files for the output shape and
    for array_match, array_keys, ignore, abs_tol, rel_tol and strip_comments. For CSV text each row
    becomes an object, so array_keys can match rows by id; use csv_diff on files for large CSVs.
    """
    return _run_diff(a, b, "text", format, array_match, array_keys, ignore, abs_tol, rel_tol,
                     strip_comments, "auto", max_changes, max_value_chars)


@tool
def csv_diff(
    a: str,
    b: str,
    key_columns: list[str] | None = None,
    ignore_columns: list[str] | None = None,
    delimiter: str | None = None,
    encoding: str = "auto",
    abs_tol: float = 0.0,
    rel_tol: float = 0.0,
    max_changes: int = 200,
    max_value_chars: int = 200,
) -> dict[str, Any]:
    """Row-level diff of two CSV/TSV files: added, removed and changed rows, per-cell changes, columns.

    Rows are paired by key_columns (e.g. ["id"] or ["country", "year"]); without key_columns whole
    rows are compared as an unordered set, so an edited row appears as one removed plus one added
    row. Reports added/removed columns separately (cells of those columns are not compared) and
    whether the common columns were reordered. Duplicate keys are paired in file order and
    reported as a warning. The delimiter (, ; tab |) and BOM/UTF-16 encodings are detected
    automatically. File a is indexed in memory (limit 2 million rows); file b is streamed.

    Args:
        a, b: file paths.
        key_columns: column names identifying a row; they must exist in both files.
        ignore_columns: column names or globs (e.g. "updated_*") whose cells are not compared.
        delimiter: a single character; auto-detected when omitted.
        abs_tol, rel_tol: numeric cells (both sides parse as numbers) within tolerance are equal;
            requires key_columns.
        max_changes: cap on entries in each of added_rows, removed_rows and changed_rows;
            summary counts are always complete.
    """
    _check_caps(max_changes, max_value_chars)
    if delimiter == "\\t":
        delimiter = "\t"
    return csvdiff.csv_diff(
        sandbox.resolve(a), sandbox.resolve(b), key_columns, ignore_columns, delimiter, encoding,
        abs_tol, rel_tol, max_changes, max_value_chars,
    )


@tool
def as_json_patch(
    a: str,
    b: str,
    inputs: Inputs = "files",
    format: Fmt = "auto",
    array_match: ArrayMatch = "auto",
    array_keys: list[str] | None = None,
    ignore: list[str] | None = None,
    abs_tol: float = 0.0,
    rel_tol: float = 0.0,
    strip_comments: bool = False,
    encoding: str = "auto",
) -> dict[str, Any]:
    """Generate an RFC 6902 JSON Patch that transforms document a into document b.

    The patch is verified before it is returned: it is applied to a and the result is re-diffed
    against b (using the same options), so a patch that does not reproduce b is never returned.
    Lists are patched with remove/add/move so keyed items keep their identity. With ignore or
    tolerance options the patched a equals b only up to those options. NaN/Infinity cannot be
    expressed in JSON and are rejected.

    Args:
        a, b: file paths, or the documents themselves when inputs='text'.
        inputs: 'files' (default) or 'text'; text requires an explicit format.
        format, array_match, array_keys, ignore, abs_tol, rel_tol, strip_comments, encoding:
            as in diff_files.
    """
    opts = _options(array_match, array_keys, ignore, abs_tol, rel_tol)
    pa, pb, fmts = _load_pair(a, b, inputs, format, strip_comments, encoding)
    for label, p in (("a", pa), ("b", pb)):
        if parse.has_nonfinite(p.data):
            raise StructDiffError(f"{label} contains NaN or Infinity, which JSON Patch cannot represent")
    differ = Differ(opts)
    ops = differ.patch(pa.data, pb.data)
    result = apply_patch(pa.data, ops)
    leftover = Differ(opts).diff(result, pb.data)
    if leftover and not any("is not patched" in w for w in differ.warnings):
        raise RuntimeError(f"internal error: generated patch does not reproduce b ({leftover[:3]})")
    out: dict[str, Any] = {
        "format_a": fmts[0],
        "format_b": fmts[1],
        "operations": len(ops),
        "verified": not leftover,
        "patch": ops,
    }
    warnings = [f"a: {w}" for w in pa.warnings] + [f"b: {w}" for w in pb.warnings] + differ.warnings
    if warnings:
        out["warnings"] = warnings
    return out


@tool
def apply_preview(
    a: str,
    patch: list[dict[str, Any]] | str,
    inputs: Inputs = "files",
    format: Fmt = "auto",
    strip_comments: bool = False,
    encoding: str = "auto",
    max_result_chars: int = 20000,
) -> dict[str, Any]:
    """Preview what an RFC 6902 JSON Patch does to a document, without writing anything.

    Supports add, remove, replace, move, copy and test. Returns the patched document and a
    semantic summary of what changed versus the original. A failing operation is reported with
    its index and reason, and nothing is returned in that case.

    Args:
        a: file path, or the document itself when inputs='text'.
        patch: the patch as a JSON array (or a JSON string of one).
        inputs, format, strip_comments, encoding: as in diff_files.
        max_result_chars: the result is cut off after this many characters (the summary is not).
    """
    ops: Any = patch
    if isinstance(patch, str):
        try:
            ops = json.loads(patch)
        except json.JSONDecodeError as e:
            raise StructDiffError(f"patch is not valid JSON: {e.msg} at line {e.lineno} column {e.colno}") from None
    if inputs == "files":
        pa, fa = _load_file(a, format, strip_comments, encoding, "a")
    else:
        pa, fa = _load_text(a, format, strip_comments, "a")
    result = apply_patch(pa.data, ops)
    report = Report(Differ(Options(array_match="index")).diff(pa.data, result)).to_dict(50, 120)
    text = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    out: dict[str, Any] = {
        "format_a": fa,
        "operations_applied": len(ops),
        "changed_vs_original": not strict_equal(pa.data, result),
        "summary": report["summary"],
        "result_kind": kind(result),
    }
    if len(text) > max_result_chars:
        out["result_truncated"] = True
        out["result_text"] = text[:max_result_chars]
    else:
        out["result"] = json.loads(text)
    if pa.warnings:
        out["warnings"] = [f"a: {w}" for w in pa.warnings]
    return out


@tool
def inspect_file(path: str, format: Fmt = "auto", strip_comments: bool = False, encoding: str = "auto") -> dict[str, Any]:
    """Check how a file parses before diffing it: format, encoding, top-level shape, warnings.

    Surfaces things that make diffs surprising: duplicate keys (JSON, YAML, .env), multi-document
    YAML, comments in JSON, an unexpected delimiter or encoding in CSV. For CSV it reports the
    columns, the detected delimiter and the row count (streamed, so large files are fine).
    """
    real = sandbox.resolve(path)
    used = parse.detect_format(real) if format == "auto" else format
    out: dict[str, Any] = {"path": real, "format": used, "bytes": os.path.getsize(real)}
    if used == "csv":
        f, rows, side = csvdiff.open_csv(real, "file", encoding, None)
        try:
            n = sum(1 for _ in rows)
        finally:
            f.close()
        out.update({"encoding": side.encoding, "delimiter": side.delimiter, "columns": side.header, "rows": n})
        return out
    parsed, _ = _load_file(path, format, strip_comments, encoding, "file")
    data = parsed.data
    out["top_level"] = kind(data)
    if isinstance(data, (dict, list)):
        out["entries"] = len(data)
    if isinstance(data, dict):
        out["keys"] = list(data)[:50]
    if parsed.warnings:
        out["warnings"] = parsed.warnings
    return out


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="structdiff-mcp",
        description="MCP server for semantic diffs of structured files. Read-only; files outside --dir are refused.",
    )
    ap.add_argument(
        "--dir",
        action="append",
        default=[],
        metavar="PATH",
        help="directory the server may read (repeatable; default: the current directory). "
        "Relative file paths resolve against the first one.",
    )
    args = ap.parse_args()
    try:
        sandbox.configure(args.dir or [os.getcwd()])
    except StructDiffError as e:
        ap.error(str(e))
    mcp.run()


if __name__ == "__main__":
    main()
