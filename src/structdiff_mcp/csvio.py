"""CSV reading helpers: encoding detection (BOMs), delimiter sniffing, row iteration."""

from __future__ import annotations

import codecs
import csv
import io
from collections.abc import Iterator
from typing import TextIO

from .errors import StructDiffError

CANDIDATE_DELIMITERS = (",", ";", "\t", "|")


def pick_encoding(head: bytes, requested: str) -> str:
    """Choose a codec for a byte stream.

    requested == "auto": honour a BOM (UTF-8, UTF-16, UTF-32), otherwise UTF-8.
    Anything else is passed through as an explicit codec name.
    """
    if requested != "auto":
        try:
            codecs.lookup(requested)
        except LookupError:
            raise StructDiffError(f"unknown encoding {requested!r}") from None
        return requested
    if head.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        return "utf-32"
    if head.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"
    if head.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return "utf-16"
    return "utf-8"


def decode_bytes(raw: bytes, requested: str = "auto") -> tuple[str, str]:
    """Decode bytes strictly; returns (text, codec used)."""
    enc = pick_encoding(raw[:4], requested)
    try:
        return raw.decode(enc), enc
    except UnicodeDecodeError as e:
        raise StructDiffError(
            f"the data is not valid {enc} (byte {e.start}); pass encoding= explicitly, "
            "for example 'cp1252', 'latin-1' or 'utf-16'"
        ) from None


def _field_counts(sample: str, delim: str) -> list[int]:
    rows = []
    reader = csv.reader(io.StringIO(sample, newline=""), delimiter=delim)
    try:
        for row in reader:
            if row:
                rows.append(len(row))
            if len(rows) >= 20:
                break
    except csv.Error:
        return []
    return rows


def sniff_delimiter(sample: str) -> str:
    """Pick the delimiter that splits the first rows into a consistent number of
    columns (> 1). A single-column file has no delimiter; "," is then correct."""
    best = ","
    best_score = (0, 0)
    for delim in CANDIDATE_DELIMITERS:
        counts = _field_counts(sample, delim)
        if not counts or counts[0] < 2:
            continue
        consistent = sum(1 for c in counts if c == counts[0])
        score = (consistent, counts[0])
        if score > best_score:
            best, best_score = delim, score
    return best


def iter_rows(f: TextIO, delimiter: str) -> Iterator[list[str]]:
    """Yield non-blank rows, turning csv/decoding failures into StructDiffError."""
    reader = csv.reader(f, delimiter=delimiter)
    while True:
        try:
            row = next(reader)
        except StopIteration:
            return
        except csv.Error as e:
            raise StructDiffError(f"CSV parse error near line {reader.line_num}: {e}") from None
        except UnicodeDecodeError as e:
            raise StructDiffError(
                f"CSV is not valid in the chosen encoding (byte {e.start}); pass encoding="
            ) from None
        if row:
            yield row


def check_header(header: list[str], where: str) -> None:
    seen: set[str] = set()
    for h in header:
        if h in seen:
            raise StructDiffError(f"{where}: duplicate column name {h!r} in the header row")
        seen.add(h)


def parse_csv_text(text: str, delimiter: str | None = None) -> list[dict[str, str]]:
    """Parse a whole CSV into a list of {column: cell} dicts."""
    if text.startswith("﻿"):
        text = text[1:]
    delim = delimiter or sniff_delimiter(text[:65536])
    rows = iter_rows(io.StringIO(text, newline=""), delim)
    header = next(rows, None)
    if header is None:
        return []
    check_header(header, "CSV")
    out: list[dict[str, str]] = []
    for n, row in enumerate(rows, start=1):
        if len(row) > len(header):
            raise StructDiffError(f"CSV data row {n} has {len(row)} fields but the header has {len(header)}")
        row = row + [""] * (len(header) - len(row))
        out.append(dict(zip(header, row, strict=True)))
    return out
