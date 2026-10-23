import asyncio
import py_compile

import pytest

from pylingual.patcher.workspace import EditError, Workspace

ORIGINAL = """import os

def f(x):
    if x > 1:
        return x + 1
    return [y * 2 for y in range(x)]

class C:
    def g(self):
        for i in range(3):
            print(i)
"""

WRONG_COMPARE = ORIGINAL.replace("x > 1", "x >= 1")


@pytest.fixture
def files(tmp_path):
    src = tmp_path / "original.py"
    src.write_text(ORIGINAL)
    pyc = tmp_path / "original.pyc"
    py_compile.compile(str(src), cfile=str(pyc), doraise=True)
    decompiled = tmp_path / "decompiled.py"
    decompiled.write_text(WRONG_COMPARE)
    return decompiled, pyc


@pytest.fixture
def workspace(files):
    ws = Workspace(*files)
    yield ws
    ws.close()


def test_report_shows_the_first_difference(workspace):
    report = workspace.report()
    assert report.startswith("patch 0: Semantic Error")
    assert "4 [different_bytecode]     if x >= 1:" in report
    assert "(>)" in report and "(>=)" in report


def test_replace_text_solves_and_saves(workspace, files):
    decompiled, _ = files
    report = workspace.replace_text("x >= 1", "x > 1")
    assert "SOLVED" in report
    assert decompiled.read_text() == ORIGINAL
    with pytest.raises(EditError, match="already matches"):
        workspace.replace_text("x > 1", "x >= 1")


def test_replace_text_needs_a_unique_match(workspace):
    with pytest.raises(EditError, match="not found"):
        workspace.replace_text("x > 2", "x")
    with pytest.raises(EditError, match="2 times"):
        workspace.replace_text("return", "yield")
    assert len(workspace.state.patches) == 1


def test_replace_lines(workspace):
    assert "SOLVED" in workspace.replace_lines(4, 4, "    if x > 1:")
    assert workspace.source == ORIGINAL


def test_replace_lines_inserts_deletes_and_checks_range(workspace):
    with pytest.raises(EditError, match="out of range"):
        workspace.replace_lines(5, 99, "")
    # inserting and then deleting a line leaves the source as it was
    workspace.replace_lines(1, 0, "import sys")
    assert workspace.lines[0] == "import sys"
    workspace.replace_lines(1, 1, "")
    assert workspace.source == WRONG_COMPARE


def test_compile_error_shows_the_line(workspace):
    report = workspace.replace_text("if x >= 1:", "if x >= 1")
    assert "does not compile" in report and "syntax error near line 4" in report
    assert "4 |     if x >= 1" in report


def test_revert_and_history(workspace):
    workspace.replace_text("if x >= 1:", "if x >= 1")
    workspace.revert(0)
    assert workspace.source == WRONG_COMPARE
    assert "<- current" in workspace.history().split("\n")[1]
    with pytest.raises(EditError):
        workspace.revert(5)


def test_original_bytecode_lists_and_disassembles(workspace):
    assert "<module>.C.g" in workspace.original_bytecode()
    assert "FOR_ITER" in workspace.original_bytecode("<module>.C.g")
    with pytest.raises(EditError):
        workspace.original_bytecode("nope")


def test_read_source_numbers_lines(workspace):
    assert workspace.read(4, 5).split("\n") == ["4 |     if x >= 1:", "5 |         return x + 1"]


def test_server_tools(files):
    pytest.importorskip("mcp")
    from mcp.client import Client

    from pylingual.patcher.mcp_server import build_server

    decompiled, pyc = files

    async def main():
        async with Client(build_server()) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert {"open_file", "replace_text", "line_diff", "original_bytecode"} <= names

            result = await client.call_tool("open_file", {"decompiled": str(decompiled), "original_pyc": str(pyc)})
            assert not result.is_error and "workspace: w1" in result.content[0].text

            result = await client.call_tool("replace_text", {"workspace": "w1", "old": "missing", "new": "x"})
            assert result.is_error

            result = await client.call_tool("replace_text", {"workspace": "w1", "old": "x >= 1", "new": "x > 1"})
            assert "SOLVED" in result.content[0].text

            result = await client.call_tool("status", {"workspace": "w2"})
            assert result.is_error and "no open workspace" in result.content[0].text

    asyncio.run(main())
    assert decompiled.read_text() == ORIGINAL
