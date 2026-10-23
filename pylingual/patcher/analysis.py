from __future__ import annotations

import ast
import difflib
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from pylingual.decompiler import lno_regex, unclosed_bracket_line
from pylingual.editable_bytecode import EditableBytecode, Inst
from pylingual.equivalence_check import PatchedPYC, compare_pyc, matching_iter
from pylingual.utils.generate_bytecode import CompileError, PyenvError, compile_version
from pylingual.utils.version import PythonVersion

# error kinds shown by the interface, matching the ones pylingual.io highlights
RESULT_ERROR_KINDS = {
    "Different bytecode": "different_bytecode",
    "Different control flow": "different_control_flow",
    "Missing bytecode": "missing_bytecode",
    "Extra bytecode": "extra_bytecode",
}


def _key(inst: Inst) -> str:
    """Comparison key for aligning instructions; jump targets are left out since offsets shift with any edit above them"""
    if inst.is_jump:
        return inst.opname
    if hasattr(inst.argval, "co_code"):
        return f"{inst.opname} code object {inst.argval.co_name}"
    return f"{inst.opname} {inst.argrepr}"


def _line_of_each(bytecode: EditableBytecode) -> list[int | None]:
    """The source line of every instruction, carrying starts_line forward and giving leading instructions the first line"""
    lines = []
    current = None
    for inst in bytecode:
        if inst.starts_line is not None:
            current = inst.starts_line
        lines.append(current)
    first = next((x for x in lines if x is not None), None)
    return [first if x is None else x for x in lines]


def statement_spans(source: str) -> dict[int, tuple[int, int]]:
    """Map each line to the first and last line of the statement it belongs to; compound statements only cover their header.
    Empty when the running Python cannot parse the source, so callers fall back to single lines."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return {}
    spans = {}
    # ast.walk is breadth first, so nested statements come later and claim the lines they share with their parent
    for node in ast.walk(tree):
        if not isinstance(node, (ast.stmt, ast.excepthandler)):
            continue
        start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
        end = node.end_lineno or node.lineno
        block = getattr(node, "body", None) or [case.pattern for case in getattr(node, "cases", [])]
        if isinstance(block, list) and block:
            end = max(node.lineno, block[0].lineno - 1)
        for line in range(start, end + 1):
            spans[line] = (start, end)
    return spans


@dataclass
class LineGroup:
    """Instructions from one code object that belong to a single statement of the patched source"""

    line: int | None
    original: list[Inst] = field(default_factory=list)
    reconstructed: list[Inst] = field(default_factory=list)
    differs: bool = False
    # original line of whole statements the patched source does not produce, placed after the statement in `line`
    missing: int | None = None


def align_code_objects(original: EditableBytecode, reconstructed: EditableBytecode, statement_of=lambda line: line) -> list[LineGroup]:
    """Group the instructions of a matched pair of code objects by the statement of the patched source they belong to,
    with statement_of mapping a line to the first line of its statement"""
    keys_a = [_key(inst) for inst in original]
    keys_b = [_key(inst) for inst in reconstructed]
    lines_b = [statement_of(line) if line is not None else None for line in _line_of_each(reconstructed)]

    groups: list[LineGroup] = []

    def group_for(line: int | None) -> LineGroup:
        if not groups or groups[-1].line != line or groups[-1].missing is not None:
            groups.append(LineGroup(line))
        return groups[-1]

    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=keys_a, b=keys_b, autojunk=False).get_opcodes():
        differs = tag != "equal"
        if j1 == j2:
            # original instructions with nothing to pair against belong to the statement before them
            line = lines_b[j1 - 1] if j1 else (lines_b[0] if lines_b else None)
            if original[i1].starts_line is not None:
                # they start a line in the original, so whole statements are missing rather than part of this one
                groups.append(LineGroup(line, missing=original[i1].starts_line))
                group = groups[-1]
            else:
                group = group_for(line)
            group.original.extend(original[i1:i2])
            group.differs = True
            continue
        for k, j in enumerate(range(j1, j2)):
            group = group_for(lines_b[j])
            group.reconstructed.append(reconstructed[j])
            group.differs |= differs
            if i1 + k < i2:
                group.original.append(original[i1 + k])
            # leftover original instructions of a replaced run go with the last reconstructed one
            if j == j2 - 1 and i1 + k + 1 < i2:
                group.original.extend(original[i1 + k + 1 : i2])
    return groups


def _dis(insts: list[Inst]) -> str:
    return "\n".join(inst.get_dis_view() for inst in insts)


@dataclass
class BytecodePane:
    """Text for a bytecode editor plus the source line shown in the gutter for each of its lines"""

    lines: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    # gutter line -> source line, so clicking the gutter can jump to the source
    targets: list[int | None] = field(default_factory=list)

    def add(self, text: str, label: str = "", target: int | None = None):
        for i, line in enumerate(text.split("\n")):
            self.lines.append(line)
            self.labels.append(label if i == 0 else "")
            self.targets.append(target)

    def pad_to(self, length: int):
        while len(self.lines) < length:
            self.add("")

    def as_dict(self) -> dict:
        return {"text": "\n".join(self.lines), "labels": self.labels, "targets": self.targets}


def compile_error_lines(source: str, message: str) -> list[list]:
    match = lno_regex.search(message)
    if not match:
        return []
    lno = int(match.group())
    kind = "indentation" if "IndentationError" in message or "TabError" in message else "syntax"
    lno = unclosed_bracket_line(source, lno) or lno
    return [[lno, lno, kind]]


class PatchSession:
    """Compiles patched source in the original pyc's Python version and compares the result with the original"""

    def __init__(self, original_pyc: Path, version: PythonVersion | None = None):
        self.original_path = Path(original_pyc)
        self.original = PatchedPYC(self.original_path)
        self.version = PythonVersion(version) if version is not None else self.original.pyc.version
        self.tmp = tempfile.TemporaryDirectory(prefix="pylingual_patch_")
        self.compiles = 0

    def compile(self, source: str) -> Path:
        self.compiles += 1
        src = Path(self.tmp.name) / f"patch_{self.compiles}.py"
        pyc = src.with_suffix(".pyc")
        src.write_text(source, encoding="utf-8")
        try:
            compile_version(src, pyc, self.version)
        except CompileError as e:
            # the temporary file name means nothing to the user
            raise CompileError(str(e).replace(str(src), "<patch>")) from None
        return pyc

    def original_only_pane(self) -> BytecodePane:
        pane = BytecodePane()
        for bytecode in self.original.pyc.iter_bytecodes():
            pane.add(f"# {bytecode.name}")
            pane.add(_dis(list(bytecode)))
            pane.add("")
        return pane

    def analyze(self, source: str) -> dict:
        try:
            compiled = self.compile(source)
        except (CompileError, PyenvError) as e:
            message = str(e).strip()
            return {
                "python_version": self.version.as_str(),
                "compile_error": message,
                "equivalence": {"successful": False, "summary": "Compilation Error", "messages": [{"text": line, "successful": False, "line": -1} for line in message.splitlines()]},
                "line_errors": compile_error_lines(source, message),
                "line_diffs": {},
                "original_bytecode": self.original_only_pane().as_dict(),
                "reconstructed_bytecode": BytecodePane().as_dict(),
            }

        results = compare_pyc(self.original, compiled)
        # code objects shared with the original carry its line numbers, so build an unshared copy for display
        reconstructed = PatchedPYC(compiled)
        spans = statement_spans(source)

        def statement_of(line: int) -> int:
            return spans.get(line, (line, line))[0]

        def end_of(start: int) -> int:
            return spans.get(start, (start, start))[1]

        original_pane, reconstructed_pane = BytecodePane(), BytecodePane()
        line_diffs: dict[int, list[list[str]]] = {}
        line_errors: list[list] = []
        messages = []
        # compare_pyc gives one result per matched pair, in the same order as matching_iter
        for test_result, (bc_a, bc_b) in zip(results, matching_iter(self.original.pyc, reconstructed.pyc)):
            name = bc_a.name if bc_a is not None else bc_b.name
            result = None if test_result.success else test_result
            kind = RESULT_ERROR_KINDS.get(result.message, "general") if result else None
            error_line = -1

            if bc_b is None:
                original_pane.add(f"# {name} (missing from patch)")
                original_pane.add(_dis(list(bc_a)))
                original_pane.add("")
                reconstructed_pane.pad_to(len(original_pane.lines))
            elif bc_a is None:
                reconstructed_pane.add(f"# {name} (not in original)", target=bc_b.codeobj.co_firstlineno)
                lines = sorted({statement_of(x) for x in _line_of_each(bc_b) if x is not None})
                for line in lines:
                    line_errors.append([line, end_of(line), kind])
                error_line = lines[0] if lines else -1
                reconstructed_pane.add(_dis(list(bc_b)))
                reconstructed_pane.add("")
                original_pane.pad_to(len(reconstructed_pane.lines))
            else:
                original_pane.add(f"# {name}")
                reconstructed_pane.add(f"# {name}")
                for group in align_code_objects(bc_a, bc_b, statement_of):
                    label = str(group.line) if group.line is not None else ""
                    original_pane.add(_dis(group.original), label, group.line)
                    reconstructed_pane.add(_dis(group.reconstructed), label, group.line)
                    length = max(len(original_pane.lines), len(reconstructed_pane.lines))
                    original_pane.pad_to(length)
                    reconstructed_pane.pad_to(length)
                    header = f"# {name}" if group.missing is None else f"# {name}: missing after this statement (line {group.missing} of the original)"
                    if group.line is not None:
                        line_diffs.setdefault(group.line, []).append([f"{header}\n{_dis(group.original)}", f"{header}\n{_dis(group.reconstructed)}"])
                    if result and group.differs and group.line is not None:
                        line_errors.append([group.line, end_of(group.line), kind if group.missing is None else "missing_bytecode"])
                        if error_line < 0:
                            error_line = group.line
                if result and error_line < 0:
                    # the instructions line up but something else differs (exception table, jump targets, ...)
                    error_line = statement_of(result.failed_line_number or bc_b.codeobj.co_firstlineno)
                    line_errors.append([error_line, end_of(error_line), kind])
                original_pane.add("")
                reconstructed_pane.add("")

            if result:
                messages.append({"text": str(result), "successful": False, "line": error_line})
            else:
                messages.append({"text": f"{name}: Success: Equal", "successful": True, "line": -1})

        successful = all(r.success for r in results)
        # every line of a statement shows the statement's diff
        statement_diffs = {}
        for start, diffs in line_diffs.items():
            joined = ["\n\n".join(x[0] for x in diffs), "\n\n".join(x[1] for x in diffs)]
            for line in range(start, end_of(start) + 1):
                statement_diffs[line] = joined
        return {
            "python_version": self.version.as_str(),
            "compile_error": None,
            "equivalence": {"successful": successful, "summary": "Semantically Equivalent" if successful else "Semantic Error", "messages": messages},
            "line_errors": line_errors,
            "line_diffs": statement_diffs,
            "original_bytecode": original_pane.as_dict(),
            "reconstructed_bytecode": reconstructed_pane.as_dict(),
        }

    def close(self):
        self.tmp.cleanup()

