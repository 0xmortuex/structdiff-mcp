"""The diff engine: structural comparison of JSON-shaped values and RFC 6902 patch generation.

Both the human-readable diff and the patch are driven by the same list matcher, so
"what the diff says changed" and "what the patch does" cannot drift apart.
"""

from __future__ import annotations

import bisect
import copy
import json
import math
import re
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any

from .errors import StructDiffError

ARRAY_MODES = ("auto", "index", "key", "set")
DEFAULT_ARRAY_KEYS = ("id", "name", "key")

_IDENT = re.compile(r"^[\w\-]+$")


# ---------------------------------------------------------------- values


def kind(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, (int, float)):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, dict):
        return "object"
    if isinstance(v, list):
        return "array"
    raise StructDiffError(f"unsupported value type {type(v).__name__}")


def numeric_equal(x: float, y: float, abs_tol: float = 0.0, rel_tol: float = 0.0) -> bool:
    """Equality of two numbers, exact for big ints, NaN equal to NaN, optional tolerance."""
    xf = isinstance(x, float)
    yf = isinstance(y, float)
    if (xf and math.isnan(x)) or (yf and math.isnan(y)):
        return xf and yf and math.isnan(x) and math.isnan(y)
    if x == y:
        return True
    if (xf and math.isinf(x)) or (yf and math.isinf(y)):
        return False
    if not abs_tol and not rel_tol:
        return False
    fx, fy = Fraction(x), Fraction(y)
    delta = abs(fx - fy)
    return delta <= Fraction(abs_tol) or delta <= Fraction(rel_tol) * max(abs(fx), abs(fy))


def strict_equal(a: Any, b: Any) -> bool:
    """Plain deep equality with JSON semantics: 1 == 1.0, but True != 1 and "1" != 1."""
    ka = kind(a)
    if ka != kind(b):
        return False
    if ka == "object":
        return a.keys() == b.keys() and all(strict_equal(a[k], b[k]) for k in a)
    if ka == "array":
        return len(a) == len(b) and all(strict_equal(x, y) for x, y in zip(a, b, strict=True))
    if ka == "number":
        return numeric_equal(a, b)
    return bool(a == b)


def _canon(v: Any) -> Any:
    k = kind(v)
    if k == "object":
        return ("o", tuple(sorted((key, _canon(x)) for key, x in v.items())))
    if k == "array":
        return ("a", tuple(_canon(x) for x in v))
    if k == "number":
        if isinstance(v, float):
            if math.isnan(v):
                return ("nan",)
            if math.isfinite(v) and v.is_integer():
                return ("n", int(v))
        return ("n", v)
    return (k[0], v)


def _lis_members(seq: list[int]) -> set[int]:
    """Positions (indexes into seq) of one longest strictly increasing subsequence."""
    tails: list[int] = []
    tails_pos: list[int] = []
    prev = [-1] * len(seq)
    for i, x in enumerate(seq):
        k = bisect.bisect_left(tails, x)
        if k == len(tails):
            tails.append(x)
            tails_pos.append(i)
        else:
            tails[k] = x
            tails_pos[k] = i
        prev[i] = tails_pos[k - 1] if k > 0 else -1
    out: set[int] = set()
    i = tails_pos[-1] if tails_pos else -1
    while i >= 0:
        out.add(i)
        i = prev[i]
    return out


# ---------------------------------------------------------------- paths


def key_segment(k: str) -> str:
    if _IDENT.match(k):
        return "." + k
    return "['" + k.replace("\\", "\\\\").replace("'", "\\'") + "']"


def pointer(tokens: list[str | int]) -> str:
    return "".join("/" + str(t).replace("~", "~0").replace("/", "~1") for t in tokens)


def compile_ignore(patterns: tuple[str, ...]) -> list[re.Pattern[str]]:
    out = []
    for p in patterns:
        if not p.startswith(("$", "*")):
            raise StructDiffError(
                f"ignore pattern {p!r} must start with '$' (a path from the root, e.g. "
                "'$.metadata.updatedAt') or '*' (anywhere, e.g. '*.updatedAt'); '*' matches any text"
            )
        out.append(re.compile("^" + ".*".join(re.escape(s) for s in p.split("*")) + "$", re.DOTALL))
    return out


# ---------------------------------------------------------------- options


@dataclass(frozen=True)
class Options:
    array_match: str = "auto"
    array_keys: tuple[str, ...] = DEFAULT_ARRAY_KEYS
    ignore: tuple[str, ...] = ()
    abs_tol: float = 0.0
    rel_tol: float = 0.0

    def __post_init__(self) -> None:
        if self.array_match not in ARRAY_MODES:
            raise StructDiffError(f"array_match must be one of {', '.join(ARRAY_MODES)}")
        if self.abs_tol < 0 or self.rel_tol < 0 or not (
            math.isfinite(self.abs_tol) and math.isfinite(self.rel_tol)
        ):
            raise StructDiffError("abs_tol and rel_tol must be finite and >= 0")
        if not self.array_keys:
            raise StructDiffError("array_keys must name at least one field")
        compile_ignore(self.ignore)

    @property
    def fuzzy(self) -> bool:
        return bool(self.ignore or self.abs_tol or self.rel_tol)


@dataclass
class ListMatch:
    pairs: list[tuple[int, int]]
    removed: list[int]
    added: list[int]
    mode: str
    key_field: str | None = None


# ---------------------------------------------------------------- engine


class Differ:
    def __init__(self, opts: Options | None = None) -> None:
        self.opts = opts or Options()
        self._ignore = compile_ignore(self.opts.ignore)
        self.warnings: list[str] = []

    # -- helpers

    def _skip(self, path: str) -> bool:
        return any(p.match(path) for p in self._ignore)

    def _warn(self, msg: str) -> None:
        if msg not in self.warnings:
            self.warnings.append(msg)

    def _scalar_equal(self, k: str, x: Any, y: Any) -> bool:
        if k == "number":
            return numeric_equal(x, y, self.opts.abs_tol, self.opts.rel_tol)
        return bool(x == y)

    def _equal(self, x: Any, y: Any, path: str) -> bool:
        return not self._diff(x, y, path, [])

    def _find_key_field(self, a: list[Any], b: list[Any]) -> str | None:
        lists = [lst for lst in (a, b) if lst]
        if not lists:
            return None
        for f in self.opts.array_keys:
            ok = True
            for lst in lists:
                seen: set[Any] = set()
                for it in lst:
                    if not isinstance(it, dict) or f not in it:
                        ok = False
                        break
                    v = it[f]
                    if isinstance(v, bool) or not isinstance(v, (str, int, float)) or v != v:
                        ok = False
                        break
                    if v in seen:
                        ok = False
                        break
                    seen.add(v)
                if not ok:
                    break
            if ok:
                return f
        return None

    def match_list(self, a: list[Any], b: list[Any], path: str) -> ListMatch:
        mode = self.opts.array_match
        if mode in ("auto", "key"):
            f = self._find_key_field(a, b)
            if f is not None:
                bmap = {it[f]: j for j, it in enumerate(b)}
                pairs = [(i, bmap[it[f]]) for i, it in enumerate(a) if it[f] in bmap]
                pa = {i for i, _ in pairs}
                pb = {j for _, j in pairs}
                return ListMatch(
                    pairs,
                    [i for i in range(len(a)) if i not in pa],
                    [j for j in range(len(b)) if j not in pb],
                    "key",
                    f,
                )
            if mode == "key":
                self._warn(
                    f"{path}: no field among {list(self.opts.array_keys)} is present and unique in every "
                    "item of this list; matched by index instead (pass array_keys=[...] to name the key)"
                )
            mode = "index"
        if mode == "index":
            n = min(len(a), len(b))
            return ListMatch(
                [(i, i) for i in range(n)], list(range(n, len(a))), list(range(n, len(b))), "index"
            )
        return self._match_set(a, b, path)

    def _match_set(self, a: list[Any], b: list[Any], path: str) -> ListMatch:
        pairs: list[tuple[int, int]] = []
        used_b: set[int] = set()
        used_a: set[int] = set()
        if not self.opts.fuzzy:
            buckets: dict[Any, list[int]] = {}
            for j, y in enumerate(b):
                buckets.setdefault(_canon(y), []).append(j)
            for i, x in enumerate(a):
                lst = buckets.get(_canon(x))
                if lst:
                    j = lst.pop(0)
                    pairs.append((i, j))
                    used_a.add(i)
                    used_b.add(j)
        else:
            for i, x in enumerate(a):
                for j, y in enumerate(b):
                    if j not in used_b and self._equal(x, y, path + "[*]"):
                        pairs.append((i, j))
                        used_a.add(i)
                        used_b.add(j)
                        break
        return ListMatch(
            pairs,
            [i for i in range(len(a)) if i not in used_a],
            [j for j in range(len(b)) if j not in used_b],
            "set",
        )

    @staticmethod
    def _label(m: ListMatch, item: Any, idx: int) -> str:
        if m.mode == "key":
            assert m.key_field is not None
            return f"[{m.key_field}={json.dumps(item[m.key_field])}]"
        if m.mode == "set":
            return "[*]"
        return f"[{idx}]"

    # -- diff

    def diff(self, a: Any, b: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        self._diff(a, b, "$", out)
        return out

    def _diff(self, a: Any, b: Any, path: str, out: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if self._skip(path):
            return out
        ka, kb = kind(a), kind(b)
        if ka != kb:
            out.append(
                {"op": "type_changed", "path": path, "old": a, "new": b, "old_type": ka, "new_type": kb}
            )
        elif ka == "object":
            for k in a:
                p = path + key_segment(k)
                if k not in b:
                    if not self._skip(p):
                        out.append({"op": "removed", "path": p, "old": a[k]})
                else:
                    self._diff(a[k], b[k], p, out)
            for k in b:
                if k not in a:
                    p = path + key_segment(k)
                    if not self._skip(p):
                        out.append({"op": "added", "path": p, "new": b[k]})
        elif ka == "array":
            self._diff_list(a, b, path, out)
        elif not self._scalar_equal(ka, a, b):
            out.append({"op": "changed", "path": path, "old": a, "new": b})
        return out

    def _diff_list(self, a: list[Any], b: list[Any], path: str, out: list[dict[str, Any]]) -> None:
        m = self.match_list(a, b, path)
        for i in m.removed:
            p = path + self._label(m, a[i], i)
            if not self._skip(p):
                out.append({"op": "removed", "path": p, "old": a[i]})
        for j in m.added:
            p = path + self._label(m, b[j], j)
            if not self._skip(p):
                out.append({"op": "added", "path": p, "new": b[j]})
        for i, j in m.pairs:
            self._diff(a[i], b[j], path + self._label(m, a[i], i), out)
        if m.mode == "key":
            ordered = sorted(m.pairs)
            keep = _lis_members([j for _, j in ordered])
            for pos, (i, j) in enumerate(ordered):
                if pos not in keep:
                    p = path + self._label(m, a[i], i)
                    if not self._skip(p):
                        out.append({"op": "moved", "path": p, "from_index": i, "to_index": j})

    # -- RFC 6902 patch

    def patch(self, a: Any, b: Any) -> list[dict[str, Any]]:
        ops: list[dict[str, Any]] = []
        self._patch(a, b, "$", [], ops)
        return ops

    def _patch(self, a: Any, b: Any, path: str, ptr: list[str | int], ops: list[dict[str, Any]]) -> None:
        if self._skip(path):
            return
        ka, kb = kind(a), kind(b)
        if ka != kb:
            ops.append({"op": "replace", "path": pointer(ptr), "value": copy.deepcopy(b)})
        elif ka == "object":
            for k in a:
                p = path + key_segment(k)
                if k not in b:
                    if not self._skip(p):
                        ops.append({"op": "remove", "path": pointer([*ptr, k])})
                else:
                    self._patch(a[k], b[k], p, [*ptr, k], ops)
            for k in b:
                if k not in a and not self._skip(path + key_segment(k)):
                    ops.append({"op": "add", "path": pointer([*ptr, k]), "value": copy.deepcopy(b[k])})
        elif ka == "array":
            self._patch_list(a, b, path, ptr, ops)
        elif not self._scalar_equal(ka, a, b):
            ops.append({"op": "replace", "path": pointer(ptr), "value": copy.deepcopy(b)})

    def _patch_list(
        self, a: list[Any], b: list[Any], path: str, ptr: list[str | int], ops: list[dict[str, Any]]
    ) -> None:
        m = self.match_list(a, b, path)
        removed = [i for i in m.removed if not self._skip(path + self._label(m, a[i], i))]
        added = {j for j in m.added if not self._skip(path + self._label(m, b[j], j))}
        if len(removed) != len(m.removed) or len(added) != len(m.added):
            self._warn(
                f"{path}: some items of this list match an ignore pattern, so this list is not "
                "patched at all"
            )
            return
        for i in sorted(removed, reverse=True):
            ops.append({"op": "remove", "path": pointer([*ptr, i])})
        # Tokens: ("a", i) for an original item still present, ("n", j) for a new one.
        gone = set(removed)
        cur: list[tuple[str, int]] = [("a", i) for i in range(len(a)) if i not in gone]
        a_of_b = {j: i for i, j in m.pairs}
        for j in range(len(b)):
            if j in a_of_b:
                tok = ("a", a_of_b[j])
                p = cur.index(tok)
                if p != j:
                    ops.append({"op": "move", "from": pointer([*ptr, p]), "path": pointer([*ptr, j])})
                    cur.pop(p)
                    cur.insert(j, tok)
            elif j in added:
                ops.append({"op": "add", "path": pointer([*ptr, j]), "value": copy.deepcopy(b[j])})
                cur.insert(j, ("n", j))
        for i, j in m.pairs:
            self._patch(a[i], b[j], path + self._label(m, a[i], i), [*ptr, j], ops)


# ---------------------------------------------------------------- reporting


def _render(v: Any, max_chars: int) -> tuple[Any, bool]:
    text = json.dumps(v, ensure_ascii=False, default=str)
    if len(text) > max_chars:
        return text[:max_chars] + f"... ({len(text)} chars in total)", True
    return _sanitize(v), False


def _sanitize(v: Any) -> Any:
    if isinstance(v, float) and not math.isfinite(v):
        return "NaN" if math.isnan(v) else ("Infinity" if v > 0 else "-Infinity")
    if isinstance(v, dict):
        return {k: _sanitize(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_sanitize(x) for x in v]
    return v


@dataclass
class Report:
    changes: list[dict[str, Any]]
    warnings: list[str] = field(default_factory=list)

    def to_dict(self, max_changes: int, max_value_chars: int) -> dict[str, Any]:
        counts = {op: 0 for op in ("added", "removed", "changed", "type_changed", "moved")}
        for c in self.changes:
            counts[c["op"]] += 1
        shown = []
        for c in self.changes[:max_changes]:
            e: dict[str, Any] = {"op": c["op"], "path": c["path"]}
            for side in ("old", "new"):
                if side in c:
                    e[side], cut = _render(c[side], max_value_chars)
                    if cut:
                        e[side + "_truncated"] = True
            for extra in ("old_type", "new_type", "from_index", "to_index"):
                if extra in c:
                    e[extra] = c[extra]
            shown.append(e)
        out: dict[str, Any] = {
            "identical": not self.changes,
            "summary": {**counts, "total": len(self.changes)},
            "changes": shown,
        }
        if len(self.changes) > max_changes:
            out["truncated"] = f"showing {max_changes} of {len(self.changes)} changes; raise max_changes to see more"
        if self.warnings:
            out["warnings"] = self.warnings
        return out
