import io
import py_compile

import pytest

from pylingual.patcher.analysis import PatchSession, statement_spans
from pylingual.patcher.state import PatchLocked, PatchState

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


MULTILINE = """def f(x, y):
    total = max(
        x,
        y,
    )
    print(total, x)
    return total

class C:
    pass
"""


def compile_session(tmp_path, source: str) -> PatchSession:
    src = tmp_path / "original.py"
    src.write_text(source)
    pyc = tmp_path / "original.pyc"
    py_compile.compile(str(src), cfile=str(pyc), doraise=True)
    return PatchSession(pyc)


@pytest.fixture
def session(tmp_path):
    session = compile_session(tmp_path, ORIGINAL)
    yield session
    session.close()


@pytest.fixture
def multiline_session(tmp_path):
    session = compile_session(tmp_path, MULTILINE)
    yield session
    session.close()


def test_equivalent_source_has_no_errors(session):
    analysis = session.analyze(ORIGINAL)
    assert analysis["equivalence"]["successful"]
    assert analysis["line_errors"] == []
    assert all(m["successful"] for m in analysis["equivalence"]["messages"])


def test_different_bytecode_is_pinned_to_its_line(session):
    analysis = session.analyze(WRONG_COMPARE)
    assert not analysis["equivalence"]["successful"]
    assert analysis["line_errors"] == [[4, 4, "different_bytecode"]]
    failure = next(m for m in analysis["equivalence"]["messages"] if not m["successful"])
    assert "<module>.f" in failure["text"] and failure["line"] == 4
    original, reconstructed = analysis["line_diffs"][4]
    assert "(>)" in original and "(>=)" in reconstructed


def test_bytecode_panes_line_up(session):
    analysis = session.analyze(WRONG_COMPARE)
    original, reconstructed = analysis["original_bytecode"], analysis["reconstructed_bytecode"]
    assert original["text"].count("\n") == reconstructed["text"].count("\n")
    assert len(original["labels"]) == len(original["text"].split("\n"))
    # the first instruction of a line is labelled with that source line
    assert "4" in reconstructed["labels"] and "6" in reconstructed["labels"]


def test_statement_spans():
    source = """@decorator
def f(
    a,
):
    if a: return 1
    x = [
        a,
    ]
    match a:
        case 1:
            pass
    try:
        pass
    except (
        ValueError
    ):
        pass
"""
    spans = statement_spans(source)
    # compound statements cover their decorators and header but not their body
    assert spans[1] == spans[4] == (1, 4)
    # a body on the header's line belongs to the innermost statement
    assert spans[5] == (5, 5)
    assert spans[6] == spans[8] == (6, 8)
    assert spans[9] == (9, 9) and spans[11] == (11, 11)
    assert spans[14] == spans[16] == (14, 16)
    assert statement_spans("def f(:\n") == {}


def test_multiline_statement_is_one_diff(multiline_session):
    analysis = multiline_session.analyze(MULTILINE.replace("        y,", "        y + 1,"))
    assert analysis["line_errors"] == [[2, 5, "different_bytecode"]]
    assert all(analysis["line_diffs"][line] == analysis["line_diffs"][2] for line in range(2, 6))
    original, reconstructed = analysis["line_diffs"][2]
    assert "STORE_FAST 2 (total)" in original and "BINARY_OP" in reconstructed


def test_missing_statement_is_not_blamed_on_the_one_before(multiline_session):
    analysis = multiline_session.analyze(MULTILINE.split("class C")[0])
    assert [1, 1, "missing_bytecode"] in analysis["line_errors"]
    original, reconstructed = analysis["line_diffs"][1]
    assert "missing after this statement (line 9 of the original)\n" in original
    assert "LOAD_BUILD_CLASS" in original and "LOAD_BUILD_CLASS" not in reconstructed


def test_missing_argument_stays_in_its_statement(multiline_session):
    analysis = multiline_session.analyze(MULTILINE.replace("print(total, x)", "print(total)"))
    assert analysis["line_errors"] == [[6, 6, "different_bytecode"]]
    assert "missing after" not in analysis["line_diffs"][6][0]


def test_missing_code_object(session):
    analysis = session.analyze(ORIGINAL.split("class C:")[0])
    messages = [m["text"] for m in analysis["equivalence"]["messages"] if not m["successful"]]
    assert any("Missing bytecode" in m for m in messages)


def test_syntax_error_is_reported_on_its_line(session):
    analysis = session.analyze("import os\n\ndef f(x:\n    return x\n")
    assert analysis["equivalence"]["summary"] == "Compilation Error"
    assert analysis["line_errors"] == [[3, 3, "syntax"]]
    assert "<patch>" in analysis["compile_error"]


def test_indentation_error(session):
    analysis = session.analyze("def f():\nreturn 1\n")
    assert analysis["line_errors"][0][2] == "indentation"


def test_state_keeps_history_and_saves(session, tmp_path):
    decompiled = tmp_path / "decompiled.py"
    decompiled.write_text(WRONG_COMPARE)
    output = tmp_path / "patched.py"
    state = PatchState(session, decompiled, output)
    assert state.patches[0]["source"] == WRONG_COMPARE and not state.patches[0]["successful"]

    patch = state.check(ORIGINAL)
    assert patch["id"] == 1 and patch["successful"]
    assert [p["id"] for p in state.patch_list()] == [0, 1]
    assert "analysis" not in state.patch_list()[0]

    assert state.save(ORIGINAL) == output
    assert output.read_text() == ORIGINAL
    assert decompiled.read_text() == WRONG_COMPARE


def test_no_patches_after_a_match(session, tmp_path):
    decompiled = tmp_path / "decompiled.py"
    decompiled.write_text(WRONG_COMPARE)
    state = PatchState(session, decompiled, decompiled)
    assert state.solved is None
    state.check(ORIGINAL)
    assert state.solved["id"] == 1
    with pytest.raises(PatchLocked):
        state.check(WRONG_COMPARE)
    # saving always writes the matching source
    state.save(WRONG_COMPARE)
    assert decompiled.read_text() == ORIGINAL


def rendered_text(renderable) -> str:
    from rich.console import Console

    console = Console(width=140, record=True, file=io.StringIO())
    console.print(renderable)
    return console.export_text()


def run_app(state, script):
    import asyncio

    pytest.importorskip("textual")
    from pylingual.patcher.tui import PatcherApp

    async def main():
        app = PatcherApp(state)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await script(app, pilot)

    asyncio.run(main())


def test_tui_tracks_errors_through_edits_and_checks(session, tmp_path):
    decompiled = tmp_path / "decompiled.py"
    decompiled.write_text(WRONG_COMPARE)

    async def script(app, pilot):
        assert app.editor.error_rows() == {3: "different_bytecode"}
        await pilot.press("ctrl+n")
        assert app.editor.cursor_location == (3, 0)
        assert "(>=)" in rendered_text(app.query_one("#line-info").content)

        # a new line above the error moves the highlight with it
        app.editor.insert("\n", (0, 0))
        await pilot.pause()
        assert app.editor.error_rows() == {4: "different_bytecode"}

        app.editor.load_text(ORIGINAL)
        await pilot.pause()
        await pilot.press("ctrl+r")
        while app.checking:
            await pilot.pause(0.05)
        assert app.patch["successful"] and app.editor.error_rows() == {}
        assert [p["id"] for p in app.state.patch_list()] == [0, 1]

        # a matching patch is final
        assert app.editor.read_only
        await pilot.press("ctrl+r")
        await pilot.pause()
        assert not app.checking and len(app.state.patches) == 2

    run_app(PatchState(session, decompiled, decompiled), script)


def test_tui_highlights_whole_statements(multiline_session, tmp_path):
    decompiled = tmp_path / "decompiled.py"
    decompiled.write_text(MULTILINE.replace("        y,", "        y + 1,").replace("print(total, x)", "print(total)"))

    async def script(app, pilot):
        assert app.editor.error_rows() == {1: "different_bytecode", 2: "different_bytecode", 3: "different_bytecode", 4: "different_bytecode", 5: "different_bytecode"}
        # navigation stops once per statement
        await pilot.press("ctrl+n")
        assert app.editor.cursor_location == (1, 0)
        await pilot.press("ctrl+n")
        assert app.editor.cursor_location == (5, 0)
        # any line of a statement shows its diff
        app.editor.cursor_location = (3, 0)
        await pilot.pause()
        assert "BINARY_OP" in rendered_text(app.query_one("#line-info").content)

    run_app(PatchState(multiline_session, decompiled, decompiled), script)


def test_tui_asks_before_quitting_with_unsaved_changes(session, tmp_path):
    decompiled = tmp_path / "decompiled.py"
    decompiled.write_text(WRONG_COMPARE)

    async def script(app, pilot):
        app.editor.load_text(ORIGINAL)
        await pilot.pause()
        await pilot.press("ctrl+q")
        assert app.is_running and app.quit_armed
        await pilot.press("ctrl+s")
        assert decompiled.read_text() == ORIGINAL
        await pilot.press("ctrl+q")
        await pilot.pause()
        assert not app.is_running

    run_app(PatchState(session, decompiled, decompiled), script)


def test_tui_theme_and_wide_view_cover_both_panels(session, tmp_path):
    decompiled = tmp_path / "decompiled.py"
    decompiled.write_text(WRONG_COMPARE)

    async def script(app, pilot):
        app.theme = "catppuccin-latte"
        await pilot.pause()
        assert app.editor.theme == "css-light"
        app.theme = "monokai"
        await pilot.pause()
        assert app.editor.theme == "monokai"

        await pilot.press("ctrl+w")
        assert app.screen.maximized is app.editor
        # the line diff brings the side panel back
        await pilot.press("ctrl+l")
        assert app.screen.maximized is None and app.query_one("#side").active == "line"

        app.query_one("#messages").focus()
        await pilot.press("ctrl+w")
        assert app.screen.maximized is app.query_one("#side")
        await pilot.press("ctrl+w")
        assert app.screen.maximized is None

    run_app(PatchState(session, decompiled, decompiled), script)
