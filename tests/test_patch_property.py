"""Property tests: the generated RFC 6902 patch must turn a into b, for random nested data.

Every example is checked three ways: by our own applier, by the independent `jsonpatch`
library, and by re-diffing the patched document against b.
"""

import copy

import jsonpatch
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from structdiff_mcp.diff import ARRAY_MODES, Differ, Options, strict_equal
from structdiff_mcp.errors import StructDiffError
from structdiff_mcp.patch import apply_patch

KEYS = st.one_of(st.sampled_from(["a", "b", "id", "name", "x/y", "~", "", "a.b", "0"]), st.text(max_size=3))
SCALARS = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(-(10**20), 10**20),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(max_size=5),
)


def _keyed_list(children):
    return st.lists(
        st.tuples(st.integers(0, 8), st.dictionaries(KEYS.filter(lambda k: k != "id"), children, max_size=3)),
        max_size=5,
        unique_by=lambda t: t[0],
    ).map(lambda ts: [{**d, "id": i} for i, d in ts])


VALUES = st.recursive(
    SCALARS,
    lambda c: st.one_of(
        st.lists(c, max_size=4),
        st.dictionaries(KEYS, c, max_size=4),
        _keyed_list(c),
    ),
    max_leaves=14,
)


@st.composite
def mutate(draw, v):
    """A random edit of v, so that a and b usually share structure."""
    if draw(st.integers(0, 9)) < 2:
        return copy.deepcopy(v)
    if isinstance(v, dict):
        out = {}
        for k, x in v.items():
            act = draw(st.integers(0, 4))
            if k == "id" and isinstance(x, int):
                act = 1  # keep list keys stable most of the time
            if act == 0:
                continue
            out[k] = draw(mutate(x)) if act >= 2 else copy.deepcopy(x)
        for _ in range(draw(st.integers(0, 2))):
            k = draw(KEYS)
            if k != "id":
                out[k] = draw(VALUES)
        return out
    if isinstance(v, list):
        items = [draw(mutate(x)) for x in v if draw(st.integers(0, 5)) > 0]
        if draw(st.booleans()):
            items = draw(st.permutations(items))
        for _ in range(draw(st.integers(0, 2))):
            items.insert(draw(st.integers(0, len(items))), draw(VALUES))
        return items
    return draw(VALUES)


# Plain random data rarely contains keyed lists with several items, so also start from those.
KEYED_ROOTS = _keyed_list(VALUES).filter(lambda x: len(x) >= 2)
ROOTS = st.one_of(VALUES, KEYED_ROOTS, st.dictionaries(KEYS, KEYED_ROOTS, min_size=1, max_size=2))
PAIRS = ROOTS.flatmap(lambda a: st.tuples(st.just(a), mutate(a)))
SETTINGS = settings(max_examples=1500, deadline=None, suppress_health_check=list(HealthCheck))


@pytest.mark.parametrize("mode", ARRAY_MODES)
@SETTINGS
@given(pair=PAIRS)
def test_patch_round_trip(mode, pair):
    a, b = pair
    a0, b0 = copy.deepcopy(a), copy.deepcopy(b)
    opts = Options(array_match=mode)
    ops = Differ(opts).patch(a, b)
    assert a == a0 and b == b0, "generating a patch must not modify its inputs"

    mine = apply_patch(a, ops)
    assert strict_equal(mine, b), (ops, mine)
    theirs = jsonpatch.apply_patch(a, ops, in_place=False)
    assert strict_equal(theirs, b), (ops, theirs)
    assert Differ(opts).diff(mine, b) == []
    assert a == a0, "applying a patch must not modify the input"


@pytest.mark.parametrize("mode", ["index", "key", "auto"])
@SETTINGS
@given(pair=PAIRS)
def test_diff_is_empty_exactly_when_equal(mode, pair):
    a, b = pair
    changes = Differ(Options(array_match=mode)).diff(a, b)
    assert (changes == []) == strict_equal(a, b)
    assert Differ(Options(array_match=mode)).diff(a, a) == []


@SETTINGS
@given(pair=PAIRS)
def test_set_mode_never_reports_moves_and_ignores_order(pair):
    a, b = pair
    changes = Differ(Options(array_match="set")).diff(a, b)
    assert all(c["op"] != "moved" for c in changes)
    if strict_equal(a, b):
        assert changes == []


@SETTINGS
@given(pair=PAIRS)
def test_patch_with_tolerance_matches_b_up_to_tolerance(pair):
    a, b = pair
    opts = Options(abs_tol=0.5)
    ops = Differ(opts).patch(a, b)
    assert Differ(opts).diff(apply_patch(a, ops), b) == []


def test_patch_round_trip_runs_at_least_a_thousand_examples():
    # Guards against someone lowering max_examples without noticing.
    assert SETTINGS.max_examples >= 1000


def test_patch_for_known_reorder_is_minimal_and_readable():
    a = [{"id": 1}, {"id": 2}, {"id": 3}]
    b = [{"id": 3}, {"id": 1}, {"id": 2}]
    ops = Differ().patch(a, b)
    assert ops == [{"op": "move", "from": "/2", "path": "/0"}]


def test_patch_for_nested_edit_uses_pointer_escaping():
    ops = Differ().patch({"a/b": {"~": 1}}, {"a/b": {"~": 2}})
    assert ops == [{"op": "replace", "path": "/a~1b/~0", "value": 2}]


def test_root_replace():
    assert Differ().patch([1], {"a": 1}) == [{"op": "replace", "path": "", "value": {"a": 1}}]
    assert apply_patch([1], [{"op": "replace", "path": "", "value": {"a": 1}}]) == {"a": 1}


def test_applier_rejects_bad_patches():
    for patch, msg in [
        ([{"op": "remove", "path": "/nope"}], "does not exist"),
        ([{"op": "add", "path": "/a/5", "value": 1}], "out of range"),
        ([{"op": "test", "path": "/a", "value": 2}], "test failed"),
        ([{"op": "bogus", "path": "/a"}], "unknown op"),
        ([{"op": "add", "path": "a", "value": 1}], "must be empty or start"),
        ([{"op": "move", "from": "/a", "path": "/a/x"}], "into itself"),
        ({"op": "add"}, "JSON array"),
    ]:
        with pytest.raises(StructDiffError, match=msg):
            apply_patch({"a": [1]}, patch)  # type: ignore[arg-type]


def test_applier_supports_every_rfc_op():
    doc = {"a": [1, 2, 3], "b": {"c": 1}}
    patch = [
        {"op": "test", "path": "/b/c", "value": 1},
        {"op": "copy", "from": "/b", "path": "/d"},
        {"op": "move", "from": "/a/0", "path": "/a/-"},
        {"op": "add", "path": "/a/1", "value": "x"},
        {"op": "replace", "path": "/b/c", "value": 2},
        {"op": "remove", "path": "/d/c"},
    ]
    assert apply_patch(doc, patch) == {"a": [2, "x", 3, 1], "b": {"c": 2}, "d": {}}
    assert jsonpatch.apply_patch(doc, patch) == apply_patch(doc, patch)
