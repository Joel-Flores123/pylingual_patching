from __future__ import annotations

import itertools
import logging
from pathlib import Path

import click

from pylingual.utils.version import PythonVersion

INSTRUCTIONS = """Patches decompiled Python source until it compiles to exactly the same bytecode as the original .pyc.

Workflow:
1. open_file(decompiled, original_pyc) returns a workspace id and a report of what differs.
2. Read the report: each differing line comes with the original bytecode and the bytecode your source produces for it.
   Work out which source construct produces the original instructions (e.g. COMPARE_OP (>) vs (>=), a missing
   else branch, `x = not y` vs `x = y is False`, a loop that should be a comprehension).
3. Fix it with replace_text (preferred) or replace_lines. Every edit is compiled and compared immediately and the new report is returned.
4. Repeat until the report says SOLVED. A solved source is saved automatically and further edits are refused.

Tips: fix one difference at a time, the first one usually causes the others. If an edit makes things worse, revert to an
earlier patch with revert(patch_id). Use line_diff(line) for any other line and original_bytecode(name) for code objects
that are missing from your source. Never change behaviour you were not asked to; the goal is identical bytecode."""


def build_server():
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError

    from pylingual.patcher.workspace import EditError, Workspace

    server = MCPServer("pylingual-patcher", instructions=INSTRUCTIONS)
    workspaces: dict[str, Workspace] = {}
    ids = itertools.count(1)

    def get(workspace: str) -> Workspace:
        try:
            return workspaces[workspace]
        except KeyError:
            raise ToolError(f"no open workspace {workspace!r}, open one with open_file (open: {', '.join(workspaces) or 'none'})") from None

    def run(workspace: str, action):
        try:
            return action(get(workspace))
        except EditError as e:
            raise ToolError(str(e)) from None

    @server.tool()
    def open_file(decompiled: str, original_pyc: str, output: str | None = None, python_version: str | None = None) -> str:
        """Start patching a decompiled .py file against the .pyc it was decompiled from.

        Args:
            decompiled: path of the decompiled source
            original_pyc: path of the original .pyc
            output: where the solved source is written, defaults to overwriting the decompiled file
            python_version: version to compile with, e.g. "3.12"; defaults to the version of the pyc
        """
        for path in (decompiled, original_pyc):
            if not Path(path).is_file():
                raise ToolError(f"{path} is not a file")
        try:
            ws = Workspace(Path(decompiled), Path(original_pyc), Path(output) if output else None, PythonVersion(python_version) if python_version else None)
        except Exception as e:
            raise ToolError(f"could not open {decompiled}: {e}") from None
        name = f"w{next(ids)}"
        workspaces[name] = ws
        return f"workspace: {name}\nsource: {decompiled} ({len(ws.lines)} lines)\n\n{ws.report()}"

    @server.tool()
    def status(workspace: str) -> str:
        """Report how the current source differs from the original bytecode."""
        return run(workspace, lambda ws: ws.report())

    @server.tool()
    def read_source(workspace: str, start: int = 1, end: int | None = None) -> str:
        """Show the current source with line numbers, optionally only lines start to end (inclusive, 1-based)."""
        return run(workspace, lambda ws: ws.read(start, end))

    @server.tool()
    def line_diff(workspace: str, line: int) -> str:
        """Show the original bytecode next to the bytecode the current source compiles to, for one source line."""
        return run(workspace, lambda ws: ws.line_diff(line))

    @server.tool()
    def original_bytecode(workspace: str, code_object: str | None = None) -> str:
        """List the code objects in the original pyc, or disassemble one by name (e.g. "<module>.C.g")."""
        return run(workspace, lambda ws: ws.original_bytecode(code_object))

    @server.tool()
    def replace_text(workspace: str, old: str, new: str) -> str:
        """Replace text that occurs exactly once in the current source, then compile and compare the result.

        Args:
            old: exact text to replace, including indentation; add surrounding lines if it is not unique
            new: replacement text
        """
        return run(workspace, lambda ws: ws.replace_text(old, new))

    @server.tool()
    def replace_lines(workspace: str, start: int, end: int, text: str) -> str:
        """Replace lines start to end (inclusive, 1-based) with text, then compile and compare the result.
        Use end = start - 1 to insert before line start, and an empty text to delete lines."""
        return run(workspace, lambda ws: ws.replace_lines(start, end, text))

    @server.tool()
    def write_source(workspace: str, source: str) -> str:
        """Replace the whole source, then compile and compare it. Prefer replace_text for small changes."""
        return run(workspace, lambda ws: ws.check(source))

    @server.tool()
    def history(workspace: str) -> str:
        """List every checked patch and whether it matched."""
        return run(workspace, lambda ws: ws.history())

    @server.tool()
    def revert(workspace: str, patch_id: int) -> str:
        """Make an earlier patch the current source again."""
        return run(workspace, lambda ws: ws.revert(patch_id))

    @server.tool()
    def save(workspace: str) -> str:
        """Write the current source to the output path, to keep partial progress. Solved sources are saved automatically."""
        return run(workspace, lambda ws: ws.save())

    @server.tool()
    def close(workspace: str) -> str:
        """Close a workspace and free its temporary files. Unsaved progress is lost."""
        get(workspace).close()
        del workspaces[workspace]
        return f"closed {workspace}"

    @server.prompt()
    def fix_decompilation(decompiled: str, original_pyc: str) -> str:
        """Patch a decompiled file until it compiles to the original bytecode."""
        return f"Open {decompiled} against {original_pyc} with open_file and patch it until the report says SOLVED.\n\n{INSTRUCTIONS}"

    return server


@click.command(help="Run an MCP server that lets an LLM patch decompiled source until it compiles to the original bytecode.", context_settings={"help_option_names": ["-h", "--help"]})
@click.option("-t", "--transport", default="stdio", type=click.Choice(["stdio", "streamable-http"]), show_default=True, help="stdio for clients that launch the server, streamable-http to serve over HTTP.")
@click.option("--host", default="127.0.0.1", show_default=True, help="Address to listen on with streamable-http.")
@click.option("--port", default=8000, show_default=True, help="Port to listen on with streamable-http.")
def main(transport: str, host: str, port: int):
    # stdout carries the protocol on stdio, so logs go to stderr
    logging.basicConfig(level="WARNING", format="%(message)s")
    try:
        server = build_server()
    except ModuleNotFoundError as e:
        if e.name is None or not e.name.startswith("mcp"):
            raise
        raise click.ClickException("the MCP server's dependencies are not installed, install them with `uv sync --extra mcp`") from None
    if transport == "stdio":
        server.run("stdio")
    else:
        server.run("streamable-http", host=host, port=port)


if __name__ == "__main__":
    main()
