from __future__ import annotations

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


@dataclass
class LineGroup:
    """Instructions from one code object that belong to a single line of the patched source"""

    line: int | None
    original: list[Inst] = field(default_factory=list)
    reconstructed: list[Inst] = field(default_factory=list)
    differs: bool = False


def align_code_objects(original: EditableBytecode, reconstructed: EditableBytecode) -> list[LineGroup]:
    """Group the instructions of a matched pair of code objects by the line of the patched source they belong to"""
    keys_a = [_key(inst) for inst in original]
    keys_b = [_key(inst) for inst in reconstructed]
    lines_b = _line_of_each(reconstructed)

    groups: list[LineGroup] = []

    def group_for(line: int | None) -> LineGroup:
        if not groups or groups[-1].line != line:
            groups.append(LineGroup(line))
        return groups[-1]

    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=keys_a, b=keys_b, autojunk=False).get_opcodes():
        differs = tag != "equal"
        if j1 == j2:
            # original instructions with nothing to pair against belong to the line before them
            group = group_for(lines_b[j1 - 1] if j1 else (lines_b[0] if lines_b else None))
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
                lines = sorted({x for x in _line_of_each(bc_b) if x is not None})
                for line in lines:
                    line_errors.append([line, line, kind])
                error_line = lines[0] if lines else -1
                reconstructed_pane.add(_dis(list(bc_b)))
                reconstructed_pane.add("")
                original_pane.pad_to(len(reconstructed_pane.lines))
            else:
                original_pane.add(f"# {name}")
                reconstructed_pane.add(f"# {name}")
                for group in align_code_objects(bc_a, bc_b):
                    label = str(group.line) if group.line is not None else ""
                    original_pane.add(_dis(group.original), label, group.line)
                    reconstructed_pane.add(_dis(group.reconstructed), label, group.line)
                    length = max(len(original_pane.lines), len(reconstructed_pane.lines))
                    original_pane.pad_to(length)
                    reconstructed_pane.pad_to(length)
                    if group.line is not None:
                        line_diffs.setdefault(group.line, []).append([f"# {name}\n{_dis(group.original)}", f"# {name}\n{_dis(group.reconstructed)}"])
                    if result and group.differs and group.line is not None:
                        line_errors.append([group.line, group.line, kind])
                        if error_line < 0:
                            error_line = group.line
                if result and error_line < 0:
                    # the instructions line up but something else differs (exception table, jump targets, ...)
                    error_line = result.failed_line_number or bc_b.codeobj.co_firstlineno
                    line_errors.append([error_line, error_line, kind])
                original_pane.add("")
                reconstructed_pane.add("")

            if result:
                messages.append({"text": str(result), "successful": False, "line": error_line})
            else:
                messages.append({"text": f"{name}: Success: Equal", "successful": True, "line": -1})

        successful = all(r.success for r in results)
        return {
            "python_version": self.version.as_str(),
            "compile_error": None,
            "equivalence": {"successful": successful, "summary": "Semantically Equivalent" if successful else "Semantic Error", "messages": messages},
            "line_errors": line_errors,
            "line_diffs": {line: ["\n\n".join(x[0] for x in diffs), "\n\n".join(x[1] for x in diffs)] for line, diffs in line_diffs.items()},
            "original_bytecode": original_pane.as_dict(),
            "reconstructed_bytecode": reconstructed_pane.as_dict(),
        }

    def close(self):
        self.tmp.cleanup()

