"""csv_diff on real files, including encodings and delimiters."""

import pytest

from structdiff_mcp.csvdiff import csv_diff
from structdiff_mcp.errors import StructDiffError


def write(path, text, encoding="utf-8"):
    path.write_bytes(text.encode(encoding))
    return str(path)


def run(a, b, **kw):
    args = dict(
        key_columns=None, ignore_columns=None, delimiter=None, encoding="auto",
        abs_tol=0.0, rel_tol=0.0, max_changes=200, max_value_chars=200,
    )
    args.update(kw)
    return csv_diff(a, b, **args)


def test_keyed_rows_cells_and_columns(tmp_path):
    a = write(tmp_path / "a.csv", "id,name,qty,old\n1,ann,5,x\n2,bob,7,y\n3,cy,9,z\n")
    b = write(tmp_path / "b.csv", "id,name,qty,new\n3,cy,9,n\n1,ann,6,n\n4,di,1,n\n")
    r = run(a, b, key_columns=["id"])
    assert r["columns"] == {"added": ["new"], "removed": ["old"]}
    assert r["summary"] == {
        "rows_a": 3, "rows_b": 3, "added_rows": 1, "removed_rows": 1,
        "changed_rows": 1, "changed_cells": 1, "unchanged_rows": 1,
    }
    assert r["changed_rows"] == [
        {"key": {"id": "1"}, "a_row": 1, "b_row": 2, "cells": [{"column": "qty", "old": "5", "new": "6"}]}
    ]
    assert r["added_rows"][0]["key"] == {"id": "4"}
    assert r["removed_rows"][0]["values"] == {"id": "2", "name": "bob", "qty": "7", "old": "y"}
    assert not r["identical"]


def test_column_reorder_is_reported(tmp_path):
    a = write(tmp_path / "a.csv", "id,x,y\n1,a,b\n")
    b = write(tmp_path / "b.csv", "y,id,x\nb,1,a\n")
    r = run(a, b, key_columns=["id"])
    assert r["identical"] and r["columns"]["order_changed"] is True


def test_identical_after_reordering_rows(tmp_path):
    a = write(tmp_path / "a.csv", "id,v\n1,a\n2,b\n")
    b = write(tmp_path / "b.csv", "id,v\n2,b\n1,a\n")
    assert run(a, b, key_columns=["id"])["identical"]
    assert run(a, b)["identical"]


def test_composite_keys(tmp_path):
    a = write(tmp_path / "a.csv", "c,y,v\nus,2020,1\nus,2021,2\n")
    b = write(tmp_path / "b.csv", "c,y,v\nus,2021,3\nus,2020,1\n")
    r = run(a, b, key_columns=["c", "y"])
    assert r["changed_rows"][0]["key"] == {"c": "us", "y": "2021"}


def test_duplicates_are_paired_in_order_and_warned(tmp_path):
    a = write(tmp_path / "a.csv", "id,v\n1,a\n1,b\n")
    b = write(tmp_path / "b.csv", "id,v\n1,a\n1,c\n1,d\n")
    r = run(a, b, key_columns=["id"])
    assert r["summary"]["changed_rows"] == 1 and r["summary"]["added_rows"] == 1
    assert any("duplicate key" in w for w in r["warnings"])


def test_without_keys_edit_is_remove_plus_add(tmp_path):
    a = write(tmp_path / "a.csv", "id,v\n1,a\n2,b\n")
    b = write(tmp_path / "b.csv", "id,v\n1,a\n2,B\n")
    r = run(a, b)
    assert r["summary"]["added_rows"] == 1 and r["summary"]["removed_rows"] == 1
    assert any("key_columns" in w for w in r["warnings"])


def test_bom_utf16_semicolon_and_tab(tmp_path):
    a = write(tmp_path / "a.csv", "id;name\n1;é\n", "utf-8-sig")
    b = write(tmp_path / "b.csv", "id\tname\n1\té\n", "utf-16")
    r = run(a, b, key_columns=["id"])
    assert r["identical"]
    assert any("delimiters" in w for w in r["warnings"]) and any("encodings" in w for w in r["warnings"])


def test_cp1252_needs_explicit_encoding(tmp_path):
    a = write(tmp_path / "a.csv", "id,n\n1,café\n", "cp1252")
    b = write(tmp_path / "b.csv", "id,n\n1,cafe\n", "cp1252")
    with pytest.raises(StructDiffError, match="encoding="):
        run(a, b, key_columns=["id"])
    r = run(a, b, key_columns=["id"], encoding="cp1252")
    assert r["changed_rows"][0]["cells"] == [{"column": "n", "old": "café", "new": "cafe"}]


def test_tolerance_and_ignore_columns(tmp_path):
    a = write(tmp_path / "a.csv", "id,x,updated\n1,0.30000000000000004,t1\n")
    b = write(tmp_path / "b.csv", "id,x,updated\n1,0.3,t2\n")
    assert run(a, b, key_columns=["id"])["summary"]["changed_cells"] == 2
    r = run(a, b, key_columns=["id"], abs_tol=1e-9, ignore_columns=["upd*"])
    assert r["identical"]


def test_errors_are_actionable(tmp_path):
    a = write(tmp_path / "a.csv", "id,v\n1,a\n")
    b = write(tmp_path / "b.csv", "id,v\n1,a,extra\n")
    with pytest.raises(StructDiffError, match="key column 'nope'"):
        run(a, a, key_columns=["nope"])
    with pytest.raises(StructDiffError, match="row 1 has 3 fields"):
        run(a, b, key_columns=["id"])
    with pytest.raises(StructDiffError, match="needs key_columns"):
        run(a, a, abs_tol=0.1)
    d = write(tmp_path / "d.csv", "id,id\n1,2\n")
    with pytest.raises(StructDiffError, match="duplicate column"):
        run(d, d)
    e = write(tmp_path / "e.csv", "")
    with pytest.raises(StructDiffError, match="empty"):
        run(e, e)


def test_short_rows_are_padded_with_warning(tmp_path):
    a = write(tmp_path / "a.csv", "id,v\n1\n")
    r = run(a, a, key_columns=["id"])
    assert any("fewer fields" in w for w in r["warnings"])


def test_caps_do_not_change_counts(tmp_path):
    rows = "".join(f"{i},a\n" for i in range(50))
    a = write(tmp_path / "a.csv", "id,v\n")
    b = write(tmp_path / "b.csv", "id,v\n" + rows)
    r = run(a, b, key_columns=["id"], max_changes=5)
    assert r["summary"]["added_rows"] == 50 and len(r["added_rows"]) == 5 and "truncated" in r


def test_large_file_streams(tmp_path):
    n = 200_000
    a = tmp_path / "a.csv"
    b = tmp_path / "b.csv"
    a.write_text("id,v\n" + "".join(f"{i},{i}\n" for i in range(n)), encoding="utf-8", newline="")
    b.write_text(
        "id,v\n" + "".join(f"{i},{i + (i == 777)}\n" for i in range(n)), encoding="utf-8", newline=""
    )
    r = run(str(a), str(b), key_columns=["id"])
    assert r["summary"]["changed_rows"] == 1 and r["summary"]["unchanged_rows"] == n - 1
