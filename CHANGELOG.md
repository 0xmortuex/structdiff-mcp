# Changelog

## 0.1.0

First release.

- Tools: `diff_files`, `diff_text`, `csv_diff`, `as_json_patch`, `apply_preview`, `inspect_file`.
- Formats: JSON, JSONC (comments and trailing commas), YAML (safe loader only, multi-document,
  anchors and merge keys), TOML, .env, INI, CSV/TSV.
- List matching: by key field (`id`, `name`, `key` or your own), by index, or as a set; keyed items
  that changed position are reported as `moved` using a longest-stable-subsequence, so one item
  jumping to the front is one move, not five.
- Numeric tolerance (`abs_tol`, `rel_tol`), exact big integers, NaN equals NaN, `1 == 1.0` but
  `1 != "1"` and `true != 1`.
- RFC 6902 patch generation, verified by applying it before it is returned; property tests apply
  generated patches to random documents (6000+ cases per run) with our applier and `jsonpatch`.
- CSV: delimiter sniffing, BOM/UTF-16/UTF-32 detection, keyed rows, composite keys, duplicate keys,
  column add/remove/reorder, ignore columns, streaming of the second file.
- Read-only `--dir` sandbox (symlinks resolved before the check).
