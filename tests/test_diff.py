"""Exact expected diffs for hand-written inputs."""

import pytest

from structdiff_mcp.diff import Differ, Options, numeric_equal
from structdiff_mcp.errors import StructDiffError


def d(a, b, **kw):
    return Differ(Options(**kw)).diff(a, b)


def brief(changes):
    out = []
    for c in changes:
        out.append((c["op"], c["path"], c.get("old"), c.get("new")))
    return out


def test_key_order_is_irrelevant():
    assert d({"a": 1, "b": [1, 2]}, {"b": [1, 2], "a": 1}) == []


def test_scalar_add_remove_change():
    a = {"keep": 1, "gone": 2, "chg": "x"}
    b = {"keep": 1, "new": 3, "chg": "y"}
    assert brief(d(a, b)) == [
        ("removed", "$.gone", 2, None),
        ("changed", "$.chg", "x", "y"),
        ("added", "$.new", None, 3),
    ]


def test_type_change_is_not_a_value_change():
    ch = d({"v": 1}, {"v": "1"})
    assert ch == [
        {"op": "type_changed", "path": "$.v", "old": 1, "new": "1", "old_type": "number", "new_type": "string"}
    ]
    assert d({"v": True}, {"v": 1})[0]["op"] == "type_changed"
    assert d({"v": None}, {"v": 0})[0]["op"] == "type_changed"


def test_null_versus_missing():
    assert brief(d({"a": None}, {})) == [("removed", "$.a", None, None)]
    assert brief(d({}, {"a": None})) == [("added", "$.a", None, None)]
    assert d({"a": None}, {"a": None}) == []


def test_int_and_float_same_number():
    assert d({"v": 1}, {"v": 1.0}) == []


def test_tolerance():
    assert d({"v": 0.1 + 0.2}, {"v": 0.3}) != []
    assert d({"v": 0.1 + 0.2}, {"v": 0.3}, abs_tol=1e-9) == []
    assert d({"v": 100.0}, {"v": 100.5}, rel_tol=0.01) == []
    assert d({"v": 100.0}, {"v": 102.0}, rel_tol=0.01) != []


def test_big_ints_stay_exact():
    a, b = 2**80, 2**80 + 1
    assert d({"v": a}, {"v": b}) != []
    assert d({"v": a}, {"v": b}, abs_tol=1) == []
    assert numeric_equal(10**400, 10**400 + 1, rel_tol=1e-9)  # no float overflow for huge ints
    assert not numeric_equal(10**400, 10**400 + 10**395, rel_tol=1e-9)


def test_nan_equals_nan_and_infinity():
    nan = float("nan")
    assert d({"v": nan}, {"v": nan}) == []
    assert d({"v": nan}, {"v": 1.0}) != []
    assert d({"v": float("inf")}, {"v": float("inf")}) == []
    assert d({"v": float("inf")}, {"v": float("-inf")}) != []


def test_unicode_keys_and_values():
    ch = d({"naïve": "café", "日本": 1}, {"naïve": "café", "日本": 1})
    assert [c["path"] for c in ch] == ["$.naïve"]


def test_odd_keys_use_bracket_paths():
    ch = d({"a.b": 1, "it's": 1}, {"a.b": 2, "it's": 2})
    assert [c["path"] for c in ch] == ["$['a.b']", "$['it\\'s']"]


USERS_A = [{"id": 1, "name": "ann", "age": 30}, {"id": 2, "name": "bob", "age": 25}, {"id": 3, "name": "cy"}]


def test_keyed_lists_report_moved_not_edits():
    b = [USERS_A[2], USERS_A[0], USERS_A[1]]
    ch = d({"u": USERS_A}, {"u": b})
    assert ch == [{"op": "moved", "path": "$.u[id=3]", "from_index": 2, "to_index": 0}]


def test_keyed_lists_edit_inside_matched_item():
    b = [{"id": 1, "name": "ann", "age": 31}, {"id": 2, "name": "bob", "age": 25}, {"id": 4, "name": "di"}]
    assert brief(d(USERS_A, b)) == [
        ("removed", "$[id=3]", USERS_A[2], None),
        ("added", "$[id=4]", None, b[2]),
        ("changed", "$[id=1].age", 30, 31),
    ]


def test_insert_at_front_is_one_add_with_keys_but_a_cascade_by_index():
    a = [{"id": i} for i in (1, 2, 3)]
    b = [{"id": 0}, *a]
    assert brief(d(a, b)) == [("added", "$[id=0]", None, {"id": 0})]
    cascade = d(a, b, array_match="index")
    assert len(cascade) == 4  # three changed ids plus one appended item


def test_key_field_choice_and_fallback_warning():
    a = [{"sku": "x", "n": 1}, {"sku": "y", "n": 2}]
    b = [{"sku": "y", "n": 2}, {"sku": "x", "n": 5}]
    assert brief(d(a, b, array_keys=("sku",))) == [
        ("changed", '$[sku="x"].n', 1, 5),
        ("moved", '$[sku="x"]', None, None),
    ]
    differ = Differ(Options(array_match="key"))
    differ.diff(a, b)
    assert "matched by index" in differ.warnings[0]


def test_auto_without_key_matches_by_index_silently():
    differ = Differ(Options())
    assert brief(differ.diff([1, 2], [1, 3])) == [("changed", "$[1]", 2, 3)]
    assert differ.warnings == []


def test_duplicate_key_values_disable_key_matching():
    a = [{"id": 1, "v": "a"}, {"id": 1, "v": "b"}]
    assert brief(d(a, [{"id": 1, "v": "a"}, {"id": 1, "v": "c"}])) == [("changed", "$[1].v", "b", "c")]


def test_set_mode_ignores_order_and_counts_duplicates():
    assert d(["a", "b", "c"], ["c", "a", "b"], array_match="set") == []
    ch = d(["a", "a", "b"], ["a", "b"], array_match="set")
    assert brief(ch) == [("removed", "$[*]", "a", None)]
    ch = d([{"x": 1}, {"y": 2}], [{"y": 2}, {"x": 1}, {"z": 3}], array_match="set")
    assert brief(ch) == [("added", "$[*]", None, {"z": 3})]


def test_set_mode_with_tolerance_pairs_close_numbers():
    assert d([0.3], [0.1 + 0.2], array_match="set", abs_tol=1e-9) == []


def test_ignore_patterns():
    a = {"meta": {"ts": 1, "who": "a"}, "items": [{"id": 1, "etag": "x", "v": 1}], "ts": 5}
    b = {"meta": {"ts": 2, "who": "b"}, "items": [{"id": 1, "etag": "y", "v": 2}], "ts": 6}
    assert [c["path"] for c in d(a, b)] == ["$.meta.ts", "$.meta.who", "$.items[id=1].etag", "$.items[id=1].v", "$.ts"]
    assert [c["path"] for c in d(a, b, ignore=("$.meta.*", "$.items[*].etag", "$.ts"))] == ["$.items[id=1].v"]
    assert [c["path"] for c in d(a, b, ignore=("*.ts", "*.etag"))] == ["$.meta.who", "$.items[id=1].v"]


def test_ignore_must_be_anchored():
    with pytest.raises(StructDiffError, match="must start with"):
        Options(ignore=("ts",))


def test_bad_options():
    with pytest.raises(StructDiffError):
        Options(array_match="nope")
    with pytest.raises(StructDiffError):
        Options(abs_tol=-1)


def test_root_type_change():
    assert d([1], {"a": 1})[0]["op"] == "type_changed"
    assert d([1], {"a": 1})[0]["path"] == "$"


def test_moved_uses_longest_stable_subsequence():
    a = [{"id": i} for i in range(6)]
    b = [a[5], *a[:5]]  # one item jumps to the front; the other five keep their relative order
    ch = d(a, b)
    assert [(c["op"], c["path"]) for c in ch] == [("moved", "$[id=5]")]
