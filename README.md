# structdiff-mcp

[![CI](https://github.com/0xmortuex/structdiff-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/0xmortuex/structdiff-mcp/actions/workflows/ci.yml)

An MCP server that tells an AI agent what **actually changed** between two JSON / YAML / TOML /
CSV / .env / INI files, by path, ignoring key order and formatting. A line diff of two configs
shows 400 changed lines because someone re-sorted the keys; this shows the one port that changed.

**Why this one, when other diff MCP servers exist?** Servers such as `confdiff-mcp`, `diff-mcp` and
`mcp-config-tools` (Node/npm) already diff configs. This one is Python (`uvx structdiff-mcp`, no Node)
and goes further on the cases that make diffs lie:

- **Lists are matched by identity, not position.** `users[id=3]` moved to the front is one `moved`
  entry, not a cascade of edits. Match by `id`/`name`/`key` (or your own field), by index, or as a set.
- **A JSON Patch you can trust.** `as_json_patch` returns an RFC 6902 patch that is verified by
  applying it before it is returned, and the test suite applies generated patches to thousands of random
  documents with two independent appliers (ours and `jsonpatch`).
- **Honest about types.** `1` vs `"1"` is a `type_changed`, `null` vs a missing key is an add/remove,
  big integers stay exact, `NaN` equals `NaN`, `0.1+0.2` vs `0.3` is equal with a tolerance.
- **Warns about traps** instead of silently picking one: duplicate JSON/YAML/.env keys, multi-document
  YAML, comments in JSON, mixed CSV delimiters or encodings.
- **Keyed CSV diffing** that streams the second file: added/removed/changed rows, per-cell changes,
  column additions, duplicate keys.
- **Read-only and sandboxed** to the folders you allow; YAML is parsed with the safe loader only.

## Install

Claude Code:

```
claude mcp add structdiff -- uvx structdiff-mcp --dir "C:\path\to\your\projects"
```

Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "structdiff": {
      "command": "uvx",
      "args": ["structdiff-mcp", "--dir", "C:\\path\\to\\your\\projects"]
    }
  }
}
```

`--dir` may be repeated. Without it the server can read only the folder it was started in.
Relative file paths resolve against the first `--dir`.

## Tools

| Tool | What it does |
|---|---|
| `diff_files` | Semantic diff of two files (format auto-detected; a and b may differ in format) |
| `diff_text` | The same for two pasted strings (format required) |
| `csv_diff` | Row-level CSV/TSV diff using `key_columns`: added/removed/changed rows, cells, columns |
| `as_json_patch` | RFC 6902 patch turning a into b, verified before it is returned |
| `apply_preview` | Show what a JSON Patch does to a document (nothing is written) |
| `inspect_file` | How a file parses: format, encoding, shape, and warnings (duplicate keys, multi-doc, ...) |

Example result of `diff_text` on two JSON documents (`abs_tol=1e-9`):

```json
{
  "identical": false,
  "summary": {"added": 0, "removed": 0, "changed": 1, "type_changed": 1, "moved": 1, "total": 3},
  "changes": [
    {"op": "type_changed", "path": "$.port", "old": 8080, "new": "8080", "old_type": "number", "new_type": "string"},
    {"op": "changed", "path": "$.users[id=1].role", "old": "admin", "new": "owner"},
    {"op": "moved", "path": "$.users[id=3]", "from_index": 2, "to_index": 0}
  ]
}
```

### Options shared by the diff tools

- `array_match`: `auto` (default: key field if one is present and unique in every item, else position),
  `index`, `key` (like auto, but warns when no key works), `set` (order-insensitive, duplicate-aware).
- `array_keys`: candidate key fields, default `["id", "name", "key"]`.
- `ignore`: paths to skip, e.g. `["$.metadata.updatedAt", "$.items[*].etag", "*.timestamp"]`. `*` matches any
  text including dots and brackets; patterns must start with `$` or `*`.
- `abs_tol`, `rel_tol`: numeric tolerance. `1` and `1.0` are always equal.
- `strip_comments`: allow `//`, `/* */` and trailing commas in JSON (automatic for `.jsonc`).
- `encoding`: `auto` honours UTF-8/16/32 BOMs, otherwise UTF-8; pass `cp1252` etc. explicitly for legacy files.
- `max_changes`, `max_value_chars`: caps on output. Summary counts are always complete.

Paths look like `$.servers[id=3].port`, `$['odd.key']`, `$.tags[*]` (set mode) or `$[2]` (by index).

## Try asking

- "Diff `deploy/prod.yaml` and `deploy/staging.yaml` and ignore the replica counts."
- "What changed between `package.json` on main and this one I pasted? Only the dependencies matter."
- "Compare `config.old.toml` and `config.new.toml`; treat floats within 1e-6 as equal."
- "Compare these two exports on `customer_id` and tell me which cells changed, and which columns are new."
- "Give me a JSON Patch that turns `a.json` into `b.json`, and show me what applying it to `c.json` would do."
- "Does `settings.json` have duplicate keys or comments that would confuse a parser?"

## Limits and behaviour worth knowing

- YAML documents are compared after loading: anchors, aliases and merge keys are resolved, dates become
  ISO strings, comments are ignored. Multi-document YAML is compared as a list of documents.
- A key `1` and a key `true` in one YAML mapping collide in Python; that is reported as a duplicate-key warning.
- INI values are strings and `[DEFAULT]` is treated as an ordinary section. `.env` values are strings.
- NaN and Infinity cannot appear in a JSON Patch (`as_json_patch` refuses); in diff output they are shown as strings.
- `as_json_patch` with `ignore` or tolerance options produces a patch whose result equals b only up to
  those options. A list containing ignored items is left unpatched, with a warning.
- Structured files are limited to 50 MB; `csv_diff` indexes file a in memory (limit 2 million rows) and
  streams file b.

## Security

- **Read-only.** No tool writes, moves or deletes anything.
- **Sandboxed.** Files must resolve (symlinks followed) inside a `--dir` folder; anything else is refused.
  Only pass folders you are happy for the agent to read.
- **Safe YAML.** Only PyYAML's `SafeLoader` is used; `!!python/...` tags are rejected. Recursive aliases and
  alias bombs are detected and refused (5 million value budget, 100 levels of nesting).
- Pasted text is limited to 10 MB. File contents are returned in diff output (truncated per value), so do not
  point the server at secrets you do not want in the conversation; use `ignore` for sensitive paths.

## Develop

```
uv sync --extra test
uv run pytest            # includes ~6000 property-test cases and a real stdio session
uv run ruff check src tests
uv run mypy --strict src
```

MIT licensed. A from-scratch tutorial that builds a minimal version of this server is in
[structdiff-mcp-the-hard-way](https://github.com/0xmortuex/structdiff-mcp-the-hard-way).
