"""Keyed row-level CSV diffing that streams the second file."""

from __future__ import annotations

import fnmatch
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, TextIO

from . import csvio
from .diff import numeric_equal
from .errors import StructDiffError

MAX_INDEXED_ROWS = 2_000_000
MAX_CELLS_PER_ROW = 25
MAX_DUP_EXAMPLES = 3


@dataclass
class CsvSide:
    path: str
    header: list[str]
    delimiter: str
    encoding: str
    rows: int = 0
    warnings: list[str] = field(default_factory=list)


def _open(path: str, encoding: str) -> tuple[TextIO, str]:
    with open(path, "rb") as raw:
        head = raw.read(4)
    enc = csvio.pick_encoding(head, encoding)
    return open(path, encoding=enc, newline=""), enc


def open_csv(path: str, label: str, encoding: str, delimiter: str | None) -> tuple[TextIO, Any, CsvSide]:
    f, enc = _open(path, encoding)
    try:
        sample = f.read(65536)
    except UnicodeDecodeError as e:
        f.close()
        raise StructDiffError(
            f"{label} is not valid {enc} (byte {e.start}); pass encoding= explicitly, e.g. 'cp1252'"
        ) from None
    f.seek(0)
    delim = delimiter or csvio.sniff_delimiter(sample)
    rows = csvio.iter_rows(f, delim)
    header = next(rows, None)
    if header is None:
        f.close()
        raise StructDiffError(f"{label} is empty (no header row)")
    if header and header[0].startswith("﻿"):
        header[0] = header[0][1:]
    csvio.check_header(header, label)
    return f, rows, CsvSide(path, header, delim, enc)


def _num(s: str) -> float | None:
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        return None


def _cell_equal(x: str, y: str, abs_tol: float, rel_tol: float) -> bool:
    if x == y:
        return True
    if not abs_tol and not rel_tol:
        return False
    nx, ny = _num(x.strip()), _num(y.strip())
    return nx is not None and ny is not None and numeric_equal(nx, ny, abs_tol, rel_tol)


def _fit(row: list[str], width: int, side: CsvSide, n: int, short: list[int]) -> list[str]:
    if len(row) > width:
        raise StructDiffError(
            f"{side.path}: data row {n} has {len(row)} fields but the header has {width}; "
            "fix the file or pass the right delimiter="
        )
    if len(row) < width:
        short.append(n)
        row = row + [""] * (width - len(row))
    return row


def csv_diff(
    path_a: str,
    path_b: str,
    key_columns: list[str] | None,
    ignore_columns: list[str] | None,
    delimiter: str | None,
    encoding: str,
    abs_tol: float,
    rel_tol: float,
    max_changes: int,
    max_value_chars: int,
) -> dict[str, Any]:
    if delimiter is not None and len(delimiter) != 1:
        raise StructDiffError("delimiter must be a single character (use '\\t' for tabs)")
    keys = list(key_columns or [])
    if (abs_tol or rel_tol) and not keys:
        raise StructDiffError("numeric tolerance on CSV needs key_columns so rows can be paired")
    fa, rows_a, side_a = open_csv(path_a, "a", encoding, delimiter)
    fb, rows_b, side_b = open_csv(path_b, "b", encoding, delimiter)
    try:
        return _run(
            rows_a, rows_b, side_a, side_b, keys, ignore_columns or [], abs_tol, rel_tol,
            max_changes, max_value_chars,
        )
    finally:
        fa.close()
        fb.close()


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + f"... ({len(s)} chars)"


def _run(
    rows_a: Any,
    rows_b: Any,
    sa: CsvSide,
    sb: CsvSide,
    keys: list[str],
    ignore_patterns: list[str],
    abs_tol: float,
    rel_tol: float,
    max_changes: int,
    max_chars: int,
) -> dict[str, Any]:
    ha, hb = sa.header, sb.header
    ia = {c: i for i, c in enumerate(ha)}
    ib = {c: i for i, c in enumerate(hb)}

    def ignored(col: str) -> bool:
        return any(fnmatch.fnmatchcase(col, p) for p in ignore_patterns)

    for kc in keys:
        for side, idx, label in ((sa, ia, "a"), (sb, ib, "b")):
            if kc not in idx:
                raise StructDiffError(
                    f"key column {kc!r} is not in file {label}; its columns are {side.header}"
                )
        if ignored(kc):
            raise StructDiffError(f"key column {kc!r} is also matched by ignore_columns")
    added_cols = [c for c in hb if c not in ia]
    removed_cols = [c for c in ha if c not in ib]
    common = [c for c in ha if c in ib and not ignored(c)]
    compare = [c for c in common if c not in keys]
    order_changed = [c for c in ha if c in ib] != [c for c in hb if c in ia]

    warnings = sa.warnings + sb.warnings
    short_a: list[int] = []
    short_b: list[int] = []

    def key_of(row: list[str], idx: dict[str, int]) -> tuple[str, ...]:
        return tuple(row[idx[k]] for k in keys)

    def key_dict(key: tuple[str, ...]) -> dict[str, str]:
        return {k: _clip(v, max_chars) for k, v in zip(keys, key, strict=True)}

    def row_dict(row: list[str], header: list[str]) -> dict[str, str]:
        return {c: _clip(v, max_chars) for c, v in zip(header, row, strict=True)}

    added: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    changed: list[dict[str, Any]] = []
    n_added = n_removed = n_changed = n_cells = n_same = 0
    dup_a: Counter[tuple[str, ...]] = Counter()
    dup_b: Counter[tuple[str, ...]] = Counter()

    if keys:
        index: dict[tuple[str, ...], list[tuple[int, list[str]]]] = {}
        for n, raw in enumerate(rows_a, start=1):
            row = _fit(raw, len(ha), sa, n, short_a)
            if n > MAX_INDEXED_ROWS:
                raise StructDiffError(
                    f"file a has more than {MAX_INDEXED_ROWS} rows; filter it down first (it is indexed in memory)"
                )
            k = key_of(row, ia)
            bucket = index.setdefault(k, [])
            if bucket:
                dup_a[k] += 1
            bucket.append((n, row))
        seen_b: set[tuple[str, ...]] = set()
        nb = 0
        for nb, raw in enumerate(rows_b, start=1):
            row = _fit(raw, len(hb), sb, nb, short_b)
            k = key_of(row, ib)
            if k in seen_b:
                dup_b[k] += 1
            seen_b.add(k)
            found = index.get(k)
            if not found:
                n_added += 1
                if len(added) < max_changes:
                    added.append({"b_row": nb, "key": key_dict(k), "values": row_dict(row, hb)})
                continue
            na, arow = found.pop(0)
            if not found:
                del index[k]
            cells = []
            for c in compare:
                x, y = arow[ia[c]], row[ib[c]]
                if not _cell_equal(x, y, abs_tol, rel_tol):
                    cells.append({"column": c, "old": _clip(x, max_chars), "new": _clip(y, max_chars)})
            if cells:
                n_changed += 1
                n_cells += len(cells)
                if len(changed) < max_changes:
                    entry: dict[str, Any] = {
                        "key": key_dict(k),
                        "a_row": na,
                        "b_row": nb,
                        "cells": cells[:MAX_CELLS_PER_ROW],
                    }
                    if len(cells) > MAX_CELLS_PER_ROW:
                        entry["cells_omitted"] = len(cells) - MAX_CELLS_PER_ROW
                    changed.append(entry)
            else:
                n_same += 1
        sb.rows = nb
        for k, bucket in index.items():
            for na, arow in bucket:
                n_removed += 1
                if len(removed) < max_changes:
                    removed.append({"a_row": na, "key": key_dict(k), "values": row_dict(arow, ha)})
        removed.sort(key=lambda e: e["a_row"])
        sa.rows = n_same + n_changed + n_removed
    else:
        counts: dict[tuple[str, ...], list[tuple[int, list[str]]]] = {}
        total_a = 0
        for n, raw in enumerate(rows_a, start=1):
            row = _fit(raw, len(ha), sa, n, short_a)
            total_a = n
            if n > MAX_INDEXED_ROWS:
                raise StructDiffError(
                    f"file a has more than {MAX_INDEXED_ROWS} rows; filter it down first (it is indexed in memory)"
                )
            counts.setdefault(tuple(row[ia[c]] for c in common), []).append((n, row))
        nb = 0
        for nb, raw in enumerate(rows_b, start=1):
            row = _fit(raw, len(hb), sb, nb, short_b)
            k = tuple(row[ib[c]] for c in common)
            found = counts.get(k)
            if found:
                found.pop(0)
                if not found:
                    del counts[k]
                n_same += 1
            else:
                n_added += 1
                if len(added) < max_changes:
                    added.append({"b_row": nb, "values": row_dict(row, hb)})
        for bucket in counts.values():
            for na, arow in bucket:
                n_removed += 1
                if len(removed) < max_changes:
                    removed.append({"a_row": na, "values": row_dict(arow, ha)})
        removed.sort(key=lambda e: e["a_row"])
        sa.rows = total_a
        sb.rows = nb
        warnings.append(
            "no key_columns given: rows were compared as an unordered multiset of whole rows, "
            "so an edited row shows up as one removed plus one added row; pass key_columns to see cell-level edits"
        )

    for label, short in (("a", short_a), ("b", short_b)):
        if short:
            warnings.append(
                f"{len(short)} row(s) in {label} have fewer fields than the header and were padded "
                f"with empty cells (first: data row {short[0]})"
            )
    for label, dups in (("a", dup_a), ("b", dup_b)):
        if dups:
            ex = [key_dict(k) for k in list(dups)[:MAX_DUP_EXAMPLES]]
            warnings.append(
                f"{sum(dups.values())} duplicate key occurrence(s) in {label} over {len(dups)} key(s), "
                f"e.g. {ex}; duplicates are paired in file order"
            )
    if sa.delimiter != sb.delimiter:
        warnings.append(f"the files use different delimiters ({sa.delimiter!r} vs {sb.delimiter!r})")
    if sa.encoding != sb.encoding:
        warnings.append(f"the files use different encodings ({sa.encoding} vs {sb.encoding})")

    columns: dict[str, Any] = {"added": added_cols, "removed": removed_cols}
    if order_changed:
        columns["order_changed"] = True
    identical = not (n_added or n_removed or n_changed or added_cols or removed_cols)
    out: dict[str, Any] = {
        "identical": identical,
        "summary": {
            "rows_a": sa.rows,
            "rows_b": sb.rows,
            "added_rows": n_added,
            "removed_rows": n_removed,
            "changed_rows": n_changed,
            "changed_cells": n_cells,
            "unchanged_rows": n_same,
        },
        "columns": columns,
        "delimiter": sa.delimiter,
        "added_rows": added,
        "removed_rows": removed,
        "changed_rows": changed,
    }
    caps = []
    if n_added > len(added):
        caps.append("added_rows")
    if n_removed > len(removed):
        caps.append("removed_rows")
    if n_changed > len(changed):
        caps.append("changed_rows")
    if caps:
        out["truncated"] = f"{', '.join(caps)} lists are capped at {max_changes} entries; counts in summary are complete"
    if warnings:
        out["warnings"] = warnings
    return out
