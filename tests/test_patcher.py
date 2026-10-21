import io
import py_compile

import pytest

from pylingual.patcher.analysis import PatchSession
from pylingual.patcher.state import PatchState

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
def session(tmp_path):
    src = tmp_path / "original.py"
    src.write_text(ORIGINAL)
    pyc = tmp_path / "original.pyc"
    py_compile.compile(str(src), cfile=str(pyc), doraise=True)
    session = PatchSession(pyc)
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

    run_app(PatchState(session, decompiled, decompiled), script)


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
