"""Parsing: every format, plus the edge cases that make diffs lie."""

import pytest

from structdiff_mcp.csvio import decode_bytes, sniff_delimiter
from structdiff_mcp.errors import StructDiffError
from structdiff_mcp.parse import detect_format, parse_text, strip_jsonc


def test_json_duplicate_keys_warn_and_last_wins():
    p = parse_text('{"a": 1, "b": 2, "a": 3}', "json")
    assert p.data == {"a": 3, "b": 2}
    assert len(p.warnings) == 1 and "'a'" in p.warnings[0]


def test_json_big_int_and_unicode_and_nan():
    p = parse_text('{"n": 123456789012345678901234567890, "s": "\\u00e9\\ud83d\\ude00", "x": NaN}', "json")
    assert p.data["n"] == 123456789012345678901234567890
    assert p.data["s"] == "\u00e9\U0001F600"
    assert p.data["x"] != p.data["x"]


def test_jsonc_comments_and_trailing_commas():
    src = '{\n // line\n "url": "http://x//y", /* block */ "a": [1, 2,],\n}\n'
    assert parse_text(src, "jsonc").data == {"url": "http://x//y", "a": [1, 2]}
    assert parse_text(src, "json", strip_comments=True).data == {"url": "http://x//y", "a": [1, 2]}
    assert strip_jsonc('"a /* not a comment */"') == '"a /* not a comment */"'


def test_json_with_comments_error_hints_at_option():
    with pytest.raises(StructDiffError, match="strip_comments"):
        parse_text('{"a": 1 // hi\n}', "json")


def test_json_error_has_position():
    with pytest.raises(StructDiffError, match="line 1 column"):
        parse_text("{", "json")


def test_yaml_anchors_and_merge_keys_resolve():
    src = "base: &b {x: 1, y: 2}\nuse:\n  <<: *b\n  y: 3\nalias: *b\n"
    p = parse_text(src, "yaml")
    assert p.data["use"] == {"x": 1, "y": 3}
    assert p.data["alias"] == {"x": 1, "y": 2}


def test_yaml_multi_document_becomes_list():
    p = parse_text("a: 1\n---\nb: 2\n", "yaml")
    assert p.data == [{"a": 1}, {"b": 2}]
    assert "multi-document" in p.warnings[0]


@pytest.mark.parametrize(
    "src",
    [
        "x: !!python/object/apply:os.system ['echo hi']",
        "x: !!python/name:os.system",
        "!!python/object:builtins.object {}",
    ],
)
def test_yaml_python_tags_are_rejected(src):
    with pytest.raises(StructDiffError, match="invalid or unsafe YAML"):
        parse_text(src, "yaml")


def test_yaml_duplicate_keys_warn():
    p = parse_text("a: 1\na: 2\n", "yaml")
    assert p.data == {"a": 2} and "duplicate" in p.warnings[0]


def test_yaml_recursive_alias_rejected():
    with pytest.raises(StructDiffError, match="recursive"):
        parse_text("a: &x [*x]\n", "yaml")


def test_yaml_alias_bomb_is_bounded():
    lines = ["a0: &a0 [x, x, x, x, x, x, x, x, x, x]"]
    for i in range(1, 12):
        lines.append(f"a{i}: &a{i} [" + ", ".join([f"*a{i - 1}"] * 10) + "]")
    with pytest.raises(StructDiffError, match="expands to more than"):
        parse_text("\n".join(lines), "yaml")


def test_yaml_dates_bytes_and_odd_keys_become_json_shapes():
    p = parse_text("d: 2024-01-02\nb: !!binary aGk=\n1: one\nnull: t\n", "yaml")
    assert p.data == {"d": "2024-01-02", "b": "aGk=", "1": "one", "null": "t"}


def test_yaml_keys_one_and_true_collide_and_warn():
    p = parse_text("1: one\ntrue: t\n", "yaml")
    assert len(p.data) == 1 and "duplicate" in p.warnings[0]


def test_toml():
    p = parse_text('title = "x"\n[a]\nn = 1\nwhen = 2024-01-02T03:04:05Z\n', "toml")
    assert p.data == {"title": "x", "a": {"n": 1, "when": "2024-01-02T03:04:05+00:00"}}
    with pytest.raises(StructDiffError, match="invalid TOML"):
        parse_text("a = ", "toml")


def test_env():
    src = '# c\nexport A=1\nB="x y # not comment"\nC=plain # comment\nD=\'raw\\n\'\nE="a\\nb"\nA=2\n'
    p = parse_text(src, "env")
    assert p.data == {"A": "2", "B": "x y # not comment", "C": "plain", "D": "raw\\n", "E": "a\nb"}
    assert "duplicate" in p.warnings[0]
    with pytest.raises(StructDiffError, match="line 1"):
        parse_text("nope", "env")


def test_ini_keeps_case_and_default_section_separate():
    p = parse_text("[DEFAULT]\nx = 1\n[Server]\nHost = a\nflag\n", "ini")
    assert p.data == {"DEFAULT": {"x": "1"}, "Server": {"Host": "a", "flag": None}}


def test_csv_text_rows_become_dicts():
    assert parse_text("a;b\n1;2\n", "csv").data == [{"a": "1", "b": "2"}]


def test_format_detection():
    assert detect_format("C:\\x\\a.YML") == "yaml"
    assert detect_format("/x/.env.production") == "env"
    assert detect_format("a.tsv") == "csv"
    with pytest.raises(StructDiffError, match="pass format="):
        detect_format("a.txt")


def test_encodings_and_boms():
    assert decode_bytes("h\u00e9llo".encode("utf-8-sig"))[0] == "h\u00e9llo"
    assert decode_bytes("h\u00e9llo".encode("utf-16"))[1] == "utf-16"
    assert decode_bytes("h\u00e9llo".encode("utf-32"))[0] == "h\u00e9llo"
    with pytest.raises(StructDiffError, match="encoding="):
        decode_bytes("h\u00e9llo".encode("cp1252"))
    assert decode_bytes("h\u00e9llo".encode("cp1252"), "cp1252")[0] == "h\u00e9llo"
    with pytest.raises(StructDiffError, match="unknown encoding"):
        decode_bytes(b"x", "nope-9")


def test_delimiter_sniffing():
    assert sniff_delimiter("a;b;c\n1;2;3\n") == ";"
    assert sniff_delimiter("a\tb\n1\t2\n") == "\t"
    assert sniff_delimiter("a|b\n1|2\n") == "|"
    assert sniff_delimiter("a,b\n1,2\n") == ","
    assert sniff_delimiter("only\n1\n") == ","
    assert sniff_delimiter('a;b\n"x,y,z";2\n') == ";"
