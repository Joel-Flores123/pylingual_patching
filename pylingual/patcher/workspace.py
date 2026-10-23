from __future__ import annotations

from pathlib import Path

from pylingual.patcher.analysis import PatchSession, statement_spans
from pylingual.patcher.state import PatchLocked, PatchState
from pylingual.utils.version import PythonVersion

# keeps reports small enough for models with short context windows
MAX_ERROR_LINES = 20
MAX_DIFF_LINES = 60
MAX_STATEMENT_LINES = 10


class EditError(Exception):
    """Raised when an edit cannot be applied to the current source"""


def _numbered(lines: list[str], start: int) -> str:
    width = len(str(start + len(lines) - 1))
    return "\n".join(f"{i:>{width}} | {line}" for i, line in enumerate(lines, start))


def _clip(text: str, limit: int = MAX_DIFF_LINES) -> str:
    lines = text.split("\n")
    if len(lines) <= limit:
        return text
    return "\n".join(lines[:limit] + [f"... {len(lines) - limit} more lines"])


class Workspace:
    """One decompiled file being patched by an agent: the current source, its checked history, and text reports about it"""

    def __init__(self, decompiled: Path, original_pyc: Path, output: Path | None = None, version: PythonVersion | None = None):
        self.session = PatchSession(original_pyc, version)
        try:
            self.state = PatchState(self.session, Path(decompiled), Path(output or decompiled))
        except BaseException:
            self.session.close()
            raise
        self.current = self.state.patches[0]

    @property
    def source(self) -> str:
        return self.current["source"]

    @property
    def lines(self) -> list[str]:
        return self.source.split("\n")

    def check(self, source: str) -> str:
        """Compile and compare a new version of the source, which becomes the current one even if it is worse"""
        try:
            self.current = self.state.check(source)
        except PatchLocked as e:
            raise EditError(f"{e}; it was saved to {self.state.output_path}, no further edits are needed") from None
        if self.current["successful"]:
            self.state.save(self.source)
        return self.report()

    def replace_lines(self, start: int, end: int, text: str) -> str:
        lines = self.lines
        if not 1 <= start <= end + 1 or end > len(lines):
            raise EditError(f"lines {start}-{end} are out of range, the source has {len(lines)} lines (use end = start - 1 to insert before line start)")
        new = text.split("\n") if text else []
        return self.check("\n".join(lines[: start - 1] + new + lines[end:]))

    def replace_text(self, old: str, new: str) -> str:
        count = self.source.count(old)
        if count != 1:
            raise EditError(f"old text must occur exactly once, it occurs {count} times; include more surrounding lines" if count else "old text was not found; read_source to see the current text, including indentation")
        return self.check(self.source.replace(old, new))

    def revert(self, patch_id: int) -> str:
        if not 0 <= patch_id < len(self.state.patches):
            raise EditError(f"there is no patch {patch_id}, the history has patches 0-{len(self.state.patches) - 1}")
        self.current = self.state.patches[patch_id]
        return self.report()

    def read(self, start: int = 1, end: int | None = None) -> str:
        lines = self.lines
        end = len(lines) if end is None else min(end, len(lines))
        if not 1 <= start <= end:
            raise EditError(f"lines {start}-{end} are out of range, the source has {len(lines)} lines")
        return _numbered(lines[start - 1 : end], start)

    def statement(self, line: int) -> str:
        """The numbered lines of the statement containing line"""
        if not 0 < line <= len(self.lines):
            return f"line {line}: (out of range)"
        start, end = statement_spans(self.source).get(line, (line, line))
        return _numbered(self.lines[start - 1 : min(end, start + MAX_STATEMENT_LINES - 1)], start) + ("\n  ..." if end - start >= MAX_STATEMENT_LINES else "")

    def line_diff(self, line: int) -> str:
        analysis = self.current["analysis"]
        diff = analysis["line_diffs"].get(line)
        header = self.statement(line)
        if diff is None:
            if analysis["compile_error"]:
                return f"{header}\nno bytecode, the source does not compile:\n{analysis['compile_error']}"
            return f"{header}\nno instructions are attributed to this statement"
        original, reconstructed = diff
        return f"{header}\n\n== original bytecode ==\n{_clip(original)}\n\n== bytecode of your source ==\n{_clip(reconstructed)}"

    def original_bytecode(self, name: str | None = None) -> str:
        bytecodes = list(self.session.original.pyc.iter_bytecodes())
        if name is None:
            return "code objects in the original pyc:\n" + "\n".join(f"  {bc.name} (line {bc.codeobj.co_firstlineno})" for bc in bytecodes)
        for bc in bytecodes:
            if bc.name == name:
                return f"# {bc.name}\n" + "\n".join(inst.get_dis_view() for inst in bc)
        raise EditError(f"no code object named {name!r}; call original_bytecode without a name to list them")

    def history(self) -> str:
        lines = [f"{p['id']:>3}  {p['date']}  {p['summary']}{'  <- current' if p is self.current else ''}" for p in self.state.patches]
        return "patches (0 is the decompiler output):\n" + "\n".join(lines)

    def save(self) -> str:
        path = self.state.save(self.source)
        return f"saved patch {(self.state.solved or self.current)['id']} to {path}"

    def report(self) -> str:
        patch, analysis = self.current, self.current["analysis"]
        out = [f"patch {patch['id']}: {patch['summary']} (Python {analysis['python_version']})"]
        if patch["successful"]:
            out.append(f"SOLVED: the source compiles to the same bytecode as the original. It was saved to {self.state.output_path}; stop editing.")
            return "\n".join(out)

        if analysis["compile_error"]:
            out += ["", "the source does not compile:", analysis["compile_error"]]
            for start, _, kind in analysis["line_errors"]:
                lo = max(1, start - 2)
                out += ["", f"{kind} error near line {start}:", _numbered(self.lines[lo - 1 : start + 2], lo)]
            return "\n".join(out)

        messages = analysis["equivalence"]["messages"]
        failed = [m for m in messages if not m["successful"]]
        out += ["", f"{len(failed)} of {len(messages)} code objects differ from the original:"]
        out += [f"  {m['text'].lstrip('*')}" + (f" (line {m['line']})" if m["line"] > 0 else "") for m in failed]

        errors = sorted({(start, end, kind) for start, end, kind in analysis["line_errors"]})
        if errors:
            out += ["", "statements whose bytecode differs:"]
            for start, end, kind in errors[:MAX_ERROR_LINES]:
                text = self.lines[start - 1].rstrip() if 0 < start <= len(self.lines) else ""
                span = f"{start}-{end}" if end > start else str(start)
                out.append(f"  {span:>9} [{kind}] {text}")
            if len(errors) > MAX_ERROR_LINES:
                out.append(f"  ... and {len(errors) - MAX_ERROR_LINES} more")
            # the first difference usually explains the rest, so show it without another round trip
            out += ["", self.line_diff(errors[0][0])]
        if any(kind == "missing_bytecode" for _, _, kind in errors):
            out += ["", "missing_bytecode: the original has whole statements after that one which your source does not produce; the diff shows their instructions."]
        if any("Missing bytecode" in m["text"] for m in failed):
            out += ["", "missing code objects exist in the original but not in your source; use original_bytecode(name) to see what they should contain."]
        return "\n".join(out)

    def close(self):
        self.session.close()
