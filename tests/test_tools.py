"""The tool functions on real files in a real sandbox directory."""

import json
import os

import pytest

from structdiff_mcp import sandbox, server
from structdiff_mcp.errors import StructDiffError


@pytest.fixture
def box(tmp_path):
    root = tmp_path / "box"
    root.mkdir()
    sandbox.configure([str(root)])
    yield root
    sandbox._roots.clear()


def put(root, name, text, encoding="utf-8"):
    (root / name).write_bytes(text.encode(encoding))
    return str(root / name)


def test_diff_files_across_formats_ignores_order_and_format(box):
    put(box, "a.json", '{"b": [1, 2], "a": {"x": 1}}')
    put(box, "b.yaml", "a:\n  x: 2\nb: [1, 2]\n")
    r = server.diff_files("a.json", "b.yaml")
    assert r["format_a"] == "json" and r["format_b"] == "yaml"
    assert r["summary"] == {"added": 0, "removed": 0, "changed": 1, "type_changed": 0, "moved": 0, "total": 1}
    assert r["changes"] == [{"op": "changed", "path": "$.a.x", "old": 1, "new": 2}]


def test_json_duplicate_key_warning_reaches_the_report(box):
    put(box, "a.json", '{"k": 1, "k": 2}')
    put(box, "b.json", '{"k": 2}')
    r = server.diff_files("a.json", "b.json")
    assert r["identical"] and "duplicate" in r["warnings"][0] and r["warnings"][0].startswith("a:")


def test_keyed_array_diff_end_to_end(box):
    put(box, "a.yaml", "users:\n  - {id: 1, n: ann}\n  - {id: 2, n: bob}\n")
    put(box, "b.yaml", "users:\n  - {id: 2, n: bob}\n  - {id: 1, n: anna}\n")
    r = server.diff_files("a.yaml", "b.yaml")
    assert [(c["op"], c["path"]) for c in r["changes"]] == [("changed", "$.users[id=1].n"), ("moved", "$.users[id=1]")]
    assert r["changes"][1]["from_index"] == 0 and r["changes"][1]["to_index"] == 1


def test_truncation_caps(box):
    put(box, "a.json", json.dumps({"s": "x" * 1000, **{f"k{i}": i for i in range(30)}}))
    put(box, "b.json", json.dumps({"s": "y" * 1000, **{f"k{i}": i + 1 for i in range(30)}}))
    r = server.diff_files("a.json", "b.json", max_changes=5, max_value_chars=20)
    assert r["summary"]["total"] == 31 and len(r["changes"]) == 5 and "truncated" in r
    first = r["changes"][0]
    assert first["old_truncated"] and first["old"].startswith('"xxxx') and "chars in total" in first["old"]


def test_diff_text_needs_format_and_reports_parse_errors(box):
    r = server.diff_text('{"a": 1}', '{"a": 1.0}', format="json")
    assert r["identical"]
    with pytest.raises(StructDiffError, match="format is required"):
        server._load_text("x", "auto", False, "a")
    with pytest.raises(StructDiffError, match=r"b \(parsed as json\): invalid JSON"):
        server.diff_text("{}", "{", format="json")


def test_type_change_and_nan_render(box):
    r = server.diff_text('{"a": 1, "n": NaN}', '{"a": "1", "n": 2}', format="json")
    ops = {c["path"]: c for c in r["changes"]}
    assert ops["$.a"]["op"] == "type_changed" and ops["$.a"]["old_type"] == "number"
    assert ops["$.n"]["old"] == "NaN"
    json.dumps(r, allow_nan=False)  # the whole report is strict JSON


def test_as_json_patch_is_verified_and_applies(box):
    a = put(box, "a.json", '{"list": [{"id": 1}, {"id": 2}, {"id": 3}], "x": 1}')
    b = put(box, "b.json", '{"list": [{"id": 3}, {"id": 1, "z": true}], "y": 2}')
    r = server.as_json_patch(a, b)
    assert r["verified"] and r["operations"] == len(r["patch"])
    out = server.apply_preview(a, r["patch"])
    assert out["result"] == {"list": [{"id": 3}, {"id": 1, "z": True}], "y": 2}
    assert out["changed_vs_original"]
    again = server.apply_preview(a, json.dumps(r["patch"]))
    assert again["result"] == out["result"]


def test_as_json_patch_text_mode_and_nan_rejected(box):
    r = server.as_json_patch("[1, 2]", "[2, 1, 3]", inputs="text", format="json")
    assert r["verified"]
    with pytest.raises(StructDiffError, match="NaN"):
        server.as_json_patch("[NaN]", "[1]", inputs="text", format="json")


def test_apply_preview_failure_and_truncation(box):
    with pytest.raises(StructDiffError, match=r"operation #0 \(remove\) failed"):
        server.apply_preview('{"a": 1}', [{"op": "remove", "path": "/b"}], inputs="text", format="json")
    with pytest.raises(StructDiffError, match="patch is not valid JSON"):
        server.apply_preview('{"a": 1}', "[", inputs="text", format="json")
    big = server.apply_preview('{"a": "' + "x" * 500 + '"}', [], inputs="text", format="json", max_result_chars=50)
    assert big["result_truncated"] and len(big["result_text"]) == 50 and not big["changed_vs_original"]


def test_inspect_file(box):
    put(box, "m.yaml", "a: 1\n---\nb: 2\n")
    r = server.inspect_file("m.yaml")
    assert r["format"] == "yaml" and r["top_level"] == "array" and "multi-document" in r["warnings"][0]
    put(box, "t.csv", "a;b\n1;2\n3;4\n")
    r = server.inspect_file("t.csv")
    assert r["delimiter"] == ";" and r["rows"] == 2 and r["columns"] == ["a", "b"]


def test_csv_diff_tool(box):
    put(box, "a.csv", "id,v\n1,a\n")
    put(box, "b.csv", "id,v\n1,b\n")
    r = server.csv_diff("a.csv", "b.csv", key_columns=["id"])
    assert r["changed_rows"][0]["cells"] == [{"column": "v", "old": "a", "new": "b"}]
    put(box, "t1.tsv", "id\tv\n1\ta\n")
    r = server.csv_diff("t1.tsv", "t1.tsv", key_columns=["id"], delimiter="\\t")
    assert r["identical"] and r["delimiter"] == "\t"
    with pytest.raises(StructDiffError, match="its columns are"):
        server.csv_diff("a.csv", "b.csv", key_columns=["id"], delimiter="\\t")


def test_sandbox_blocks_escape_and_missing(box, tmp_path):
    outside = tmp_path / "secret.json"
    outside.write_text("{}", encoding="utf-8")
    put(box, "ok.json", "{}")
    for bad in (str(outside), "../secret.json", os.path.join("..", "box", "..", "secret.json")):
        with pytest.raises(StructDiffError, match="outside the allowed directories"):
            server.diff_files(bad, "ok.json")
    with pytest.raises(StructDiffError, match="does not exist"):
        server.diff_files("nope.json", "ok.json")
    (box / "d.json").mkdir()
    with pytest.raises(StructDiffError, match="not a regular file"):
        server.diff_files("d.json", "ok.json")
    with pytest.raises(StructDiffError, match="pass format="):
        put(box, "x.txt", "{}")
        server.diff_files("x.txt", "ok.json")


def test_sandbox_blocks_symlink_escape(box, tmp_path):
    outside = tmp_path / "secret.json"
    outside.write_text("{}", encoding="utf-8")
    link = box / "link.json"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("this account cannot create symlinks")
    with pytest.raises(StructDiffError, match="outside the allowed directories"):
        server.diff_files("link.json", "link.json")


def test_extra_dir_is_allowed(tmp_path):
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    (two / "f.json").write_text("{}", encoding="utf-8")
    sandbox.configure([str(one), str(two)])
    try:
        assert server.diff_files(str(two / "f.json"), str(two / "f.json"))["identical"]
    finally:
        sandbox._roots.clear()


def test_files_are_never_modified(box):
    a = put(box, "a.json", '{"a": 1}')
    b = put(box, "b.json", '{"a": 2}')
    before = (open(a, "rb").read(), open(b, "rb").read())
    server.diff_files(a, b)
    server.as_json_patch(a, b)
    server.apply_preview(a, [{"op": "replace", "path": "/a", "value": 9}])
    assert (open(a, "rb").read(), open(b, "rb").read()) == before


def test_option_validation_messages(box):
    put(box, "a.json", "{}")
    with pytest.raises(StructDiffError, match="must start with"):
        server.diff_files("a.json", "a.json", ignore=["x"])
    with pytest.raises(StructDiffError, match="max_changes"):
        server.diff_files("a.json", "a.json", max_changes=0)
    with pytest.raises(StructDiffError, match="unknown encoding"):
        server.diff_files("a.json", "a.json", encoding="bogus")


def test_jsonc_by_extension_and_encoding(box):
    put(box, "a.jsonc", '{ // c\n "a": 1, }', "utf-16")
    put(box, "b.json", '\ufeff{"a": 1}', "utf-8")
    r = server.diff_files("a.jsonc", "b.json")
    assert r["identical"]
