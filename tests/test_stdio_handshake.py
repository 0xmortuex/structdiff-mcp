"""MCP stdio smoke test: launches the installed server as a real subprocess, lists
tools, and calls them over the protocol (including the error path and the sandbox)."""

import json
import sys

import pytest

pytest.importorskip("mcp")

from mcp import ClientSession  # noqa: E402
from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: E402

EXPECTED_TOOLS = {"diff_files", "diff_text", "csv_diff", "as_json_patch", "apply_preview", "inspect_file"}


def params(root):
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "structdiff_mcp", "--dir", str(root)],
        env={"PYTHONIOENCODING": "utf-8"},
    )


def payload(res):
    assert not res.is_error, res.content[0].text
    return json.loads(res.content[0].text)


@pytest.mark.anyio
async def test_tools_and_a_session_over_stdio(tmp_path):
    (tmp_path / "a.json").write_text('{"users": [{"id": 1, "n": "x"}, {"id": 2, "n": "y"}]}', encoding="utf-8")
    (tmp_path / "b.yaml").write_text("users:\n- {id: 2, n: y}\n- {id: 1, n: z}\n", encoding="utf-8")
    (tmp_path / "a.csv").write_text("id,v\n1,a\n", encoding="utf-8")
    (tmp_path / "b.csv").write_text("id,v\n1,b\n2,c\n", encoding="utf-8")
    async with stdio_client(params(tmp_path)) as (read, write), ClientSession(read, write) as session:
        init = await session.initialize()
        assert init.server_info.name == "structdiff"
        tools = (await session.list_tools()).tools
        assert {t.name for t in tools} == EXPECTED_TOOLS
        assert all(len(t.description or "") > 200 for t in tools if t.name != "inspect_file")

        r = payload(await session.call_tool("diff_files", {"a": "a.json", "b": "b.yaml"}))
        assert [c["op"] for c in r["changes"]] == ["changed", "moved"]
        assert r["changes"][0]["path"] == "$.users[id=1].n"

        r = payload(await session.call_tool("csv_diff", {"a": "a.csv", "b": "b.csv", "key_columns": ["id"]}))
        assert r["summary"]["changed_rows"] == 1 and r["summary"]["added_rows"] == 1

        r = payload(await session.call_tool("as_json_patch", {"a": "a.json", "b": "b.yaml"}))
        assert r["verified"]
        r = payload(await session.call_tool("apply_preview", {"a": "a.json", "patch": r["patch"]}))
        assert r["result"]["users"][0] == {"id": 2, "n": "y"}

        r = payload(await session.call_tool("diff_text", {"a": "a=1\nb=2", "b": "b=2\na=3", "format": "env"}))
        assert r["changes"] == [{"op": "changed", "path": "$.a", "old": "1", "new": "3"}]


@pytest.mark.anyio
async def test_instructive_errors_reach_the_agent(tmp_path):
    (tmp_path / "ok.json").write_text("{}", encoding="utf-8")
    outside = tmp_path.parent / "outside-structdiff.json"
    outside.write_text("{}", encoding="utf-8")
    try:
        async with stdio_client(params(tmp_path)) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            res = await session.call_tool("diff_files", {"a": str(outside), "b": "ok.json"})
            assert res.is_error and "outside the allowed directories" in res.content[0].text
            res = await session.call_tool("diff_text", {"a": "{", "b": "{}", "format": "json"})
            assert res.is_error and "invalid JSON" in res.content[0].text
            res = await session.call_tool("diff_files", {"a": "ok.json", "b": "missing.json"})
            assert res.is_error and "does not exist" in res.content[0].text
            res = await session.call_tool(
                "diff_text", {"a": "x: !!python/object/apply:os.getcwd []", "b": "x: 1", "format": "yaml"}
            )
            assert res.is_error and "unsafe YAML" in res.content[0].text
    finally:
        outside.unlink()


@pytest.fixture
def anyio_backend():
    return "asyncio"
