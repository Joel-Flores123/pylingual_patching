from __future__ import annotations

import difflib
import re

from rich.console import Group
from rich.segment import Segment
from rich.style import Style
from rich.table import Table
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.strip import Strip
from textual.widgets import DataTable, Footer, Header, OptionList, Static, TabbedContent, TabPane, TextArea
from textual.widgets.option_list import Option
from textual.color import Color
from textual.widgets.text_area import Edit, EditResult, TextAreaTheme

from pylingual.patcher.state import PatchLocked, PatchState

# wording for the error kinds reported by the analysis, matching pylingual.io
KIND_TEXT = {
    "general": "Error",
    "syntax": "Syntax Error",
    "indentation": "Indentation Error",
    "different_bytecode": "Semantic Error: Different Bytecode",
    "different_control_flow": "Semantic Error: Different Control Flow",
    "missing_bytecode": "Semantic Error: Missing Bytecode",
    "extra_bytecode": "Semantic Error: Extra Bytecode",
}
KIND_COLOR = {
    "general": "#d64545",
    "syntax": "#d64545",
    "indentation": "#c77d00",
    "different_bytecode": "#d14b8f",
    "different_control_flow": "#7a4fd1",
    "missing_bytecode": "#2d8fb3",
    "extra_bytecode": "#8a6d3b",
}
# app themes that have a matching editor theme, the rest use the app's colors with a dark or light syntax palette
EDITOR_THEMES = {"monokai", "dracula"}
LIGHT_EDITOR_THEME = TextAreaTheme(name="css-light", syntax_styles=TextAreaTheme.get_builtin_theme("github_light").syntax_styles)
offset_regex = re.compile(r"^\d+ ", re.MULTILINE)


def strip_offsets(text: str) -> str:
    return offset_regex.sub("", text)


def diff_table(original: str, reconstructed: str, removed: Style, added: Style) -> Table:
    """Two column diff of bytecode listings, aligned while ignoring instruction offsets"""
    a, b = original.splitlines(), reconstructed.splitlines()
    table = Table(expand=True, show_edge=False, pad_edge=False, box=None)
    table.add_column("Original", ratio=1, overflow="fold")
    table.add_column("Reconstructed", ratio=1, overflow="fold")
    matcher = difflib.SequenceMatcher(a=[strip_offsets(x) for x in a], b=[strip_offsets(x) for x in b], autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        for k in range(max(i2 - i1, j2 - j1)):
            left = a[i1 + k] if i1 + k < i2 else ""
            right = b[j1 + k] if j1 + k < j2 else ""
            if tag == "equal":
                table.add_row(left, right)
            else:
                table.add_row(Text(left, removed if left else ""), Text(right, added if right else ""))
    return table


class LineTracker:
    """Follows lines of the checked source through later edits, so results stay on the right lines until the next check"""

    def __init__(self, lines=()):
        # line number in the checked source -> current 0-based row in the editor
        self.rows = {line: line - 1 for line in lines}

    def shift(self, to_row: int, end_row: int):
        """Move rows below an edit that replaced rows up to to_row with rows up to end_row"""
        delta = end_row - to_row
        if delta:
            for line, row in self.rows.items():
                if row > to_row:
                    self.rows[line] = row + delta
                elif row > end_row:
                    # the line was inside a deleted range
                    self.rows[line] = end_row

    def row(self, line: int) -> int:
        return self.rows.get(line, line - 1)

    def line_at(self, row: int) -> int | None:
        return next((line for line, r in self.rows.items() if r == row), None)


class SourceArea(TextArea):
    """Python editor that tints the lines the last check found problems on"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.tracker = LineTracker()
        self.errors: dict[int, str] = {}  # checked line -> error kind

    def set_results(self, tracker: LineTracker, errors: dict[int, str]):
        self.tracker = tracker
        self.errors = errors
        self._line_cache.clear()
        self.refresh()

    def error_rows(self) -> dict[int, str]:
        return {self.tracker.row(line): kind for line, kind in self.errors.items()}

    def edit(self, edit: Edit) -> EditResult:
        to_row = max(edit.from_location, edit.to_location)[0]
        result = super().edit(edit)
        self.tracker.shift(to_row, result.end_location[0])
        self._line_cache.clear()
        return result

    def line_tint(self, kind: str) -> Style:
        # rich has no transparency, so the tint is blended into the current background
        background = self.background_colors[1]
        return Style(bgcolor=background.blend(Color.parse(KIND_COLOR[kind]), 0.3).rich_color)

    def render_line(self, y: int) -> Strip:
        strip = super().render_line(y)
        kind = self.error_rows().get(self.scroll_offset.y + y)
        if kind is None:
            return strip
        # replace the theme background but leave the cursor alone so it stays visible
        cursor = self._theme.cursor_style if self._theme else None
        tint = self.line_tint(kind)
        return Strip([segment if cursor and segment.style == cursor else Segment(segment.text, (segment.style or Style()) + tint, segment.control) for segment in strip], strip.cell_length)


class SidePanel(TabbedContent):
    ALLOW_MAXIMIZE = True


class PatcherApp(App):
    ALLOW_IN_MAXIMIZED_VIEW = "Header, Footer"
    TITLE = "PyLingual Patcher"
    CSS = """
    #summary { height: 1; padding: 0 1; text-style: bold; }
    #summary.ok { background: $success-darken-2; color: $text; }
    #summary.bad { background: $error-darken-2; color: $text; }
    #body { height: 1fr; }
    #source { width: 3fr; }
    #side { width: 2fr; min-width: 40; }
    .-maximized { height: 1fr; }
    #line-info { padding: 0 1; }
    #bytecode { height: 1fr; }
    """
    BINDINGS = [
        Binding("ctrl+r", "check", "Check patch", priority=True),
        Binding("f5", "check", "Check patch", show=False, priority=True),
        Binding("ctrl+s", "save", "Save", priority=True),
        Binding("ctrl+n", "next_error", "Next error", priority=True),
        Binding("ctrl+b", "previous_error", "Prev error", priority=True),
        Binding("ctrl+l", "show_tab('line')", "Line diff", priority=True),
        Binding("ctrl+e", "focus_editor", "Editor", priority=True),
        Binding("ctrl+w", "toggle_wide", "Wide view", priority=True),
        Binding("ctrl+q", "quit", "Quit", priority=True),
    ]

    def __init__(self, state: PatchState):
        super().__init__()
        self.state = state
        self.patch = state.patches[-1]
        self.saved_source = state.output_path.read_text(encoding="utf-8") if state.output_path.exists() else None
        self.quit_armed = False
        self.checking = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="summary")
        with Horizontal(id="body"):
            yield SourceArea(self.patch["source"], language="python", id="source", soft_wrap=False, show_line_numbers=True, tab_behavior="indent")
            with SidePanel(id="side", initial="report"):
                with TabPane("Report", id="report"):
                    yield OptionList(id="messages")
                with TabPane("Line", id="line"):
                    with VerticalScroll():
                        yield Static(id="line-info")
                with TabPane("Bytecode", id="bytecode-tab"):
                    yield DataTable(id="bytecode", cursor_type="row", zebra_stripes=False)
                with TabPane("History", id="history-tab"):
                    yield OptionList(id="history")
        yield Footer()

    def on_mount(self):
        self.editor.register_theme(LIGHT_EDITOR_THEME)
        self.theme_changed_signal.subscribe(self, lambda _: self.apply_theme())
        self.apply_theme()
        self.sub_title = f"{self.state.session.original_path.name} · Python {self.patch['analysis']['python_version']}"
        self.show_patch(self.patch, replace_source=False)
        self.lock_if_solved()
        self.query_one(SourceArea).focus()

    @property
    def editor(self) -> SourceArea:
        return self.query_one(SourceArea)

    @property
    def dirty(self) -> bool:
        return self.editor.text != self.patch["source"]

    def apply_theme(self):
        theme = self.current_theme
        self.editor.theme = theme.name if theme.name in EDITOR_THEMES else ("css" if theme.dark else LIGHT_EDITOR_THEME.name)
        background = Color.parse(theme.background or ("#000000" if theme.dark else "#ffffff"))
        error, success = Color.parse(theme.error), Color.parse(theme.success)
        self.removed = Style(color=error.rich_color, bgcolor=background.blend(error, 0.2).rich_color)
        self.added = Style(color=success.rich_color, bgcolor=background.blend(success, 0.2).rich_color)
        self.fill_messages()
        self.fill_bytecode_table()
        self.update_line_info()

    def show_patch(self, patch: dict, replace_source: bool = True):
        self.patch = patch
        analysis = patch["analysis"]
        if replace_source and self.editor.text != patch["source"]:
            location = self.editor.cursor_location
            self.editor.load_text(patch["source"])
            self.editor.cursor_location = location

        lines = {line for line, _, _ in analysis["line_errors"]} | set(analysis["line_diffs"])
        errors = {}
        for start, end, kind in analysis["line_errors"]:
            for line in range(start, end + 1):
                errors.setdefault(line, kind)
        self.editor.set_results(LineTracker(lines), errors)

        self.update_summary()
        self.fill_messages()
        self.fill_bytecode_table()

        history = self.query_one("#history", OptionList)
        history.clear_options()
        for entry in reversed(self.state.patch_list()):
            name = "Initial decompilation" if entry["id"] == 0 else f"Patch {entry['id']}"
            mark = "✓" if entry["successful"] else "✗"
            current = " (shown)" if entry["id"] == patch["id"] else ""
            history.add_option(Option(f"{name} · {entry['date'][11:]} · {mark} {entry['summary']}{current}", id=f"p{entry['id']}"))
        self.update_line_info()

    def fill_messages(self):
        messages = self.query_one("#messages", OptionList)
        messages.clear_options()
        for i, message in enumerate(self.patch["analysis"]["equivalence"]["messages"]):
            style = "" if message["successful"] else Style(color=self.removed.color, bold=True)
            messages.add_option(Option(Text(message["text"], style=style), id=f"m{i}"))

    def fill_bytecode_table(self):
        analysis = self.patch["analysis"]
        table = self.query_one("#bytecode", DataTable)
        table.clear(columns=True)
        original, reconstructed = analysis["original_bytecode"], analysis["reconstructed_bytecode"]
        rows_a, rows_b = original["text"].split("\n"), reconstructed["text"].split("\n")
        # split the panel evenly so the reconstructed column is never pushed off screen
        available = (table.size.width or self.query_one("#side").size.width or 80) - 5 - 8
        width = max(12, min(available // 2, max(len(x) for x in rows_a + rows_b)))
        table.add_column("Line", key="line", width=5)
        table.add_column("Original", key="original", width=width)
        table.add_column("Reconstructed", key="reconstructed", width=width)
        for i in range(max(len(rows_a), len(rows_b))):
            left = rows_a[i] if i < len(rows_a) else ""
            right = rows_b[i] if i < len(rows_b) else ""
            label = original["labels"][i] if i < len(original["labels"]) and original["labels"][i] else (reconstructed["labels"][i] if i < len(reconstructed["labels"]) else "")
            differs = strip_offsets(left) != strip_offsets(right)
            table.add_row(Text(label, style="dim"), Text(left, self.removed if differs and left else ""), Text(right, self.added if differs and right else ""), key=str(i))

    def on_resize(self):
        self.call_after_refresh(self.fill_bytecode_table)

    def action_toggle_wide(self):
        """Fill the screen with the focused panel, or put both panels back"""
        if self.screen.maximized is not None:
            self.screen.minimize()
        else:
            self.screen.maximize(self.focused or self.query_one(SidePanel))
        self.call_after_refresh(self.fill_bytecode_table)

    def show_editor(self):
        if self.screen.maximized not in (None, self.editor):
            self.screen.minimize()
            self.call_after_refresh(self.fill_bytecode_table)

    def show_side(self):
        if self.screen.maximized is self.editor:
            self.screen.minimize()
            self.call_after_refresh(self.fill_bytecode_table)

    def update_summary(self):
        equivalence = self.patch["analysis"]["equivalence"]
        failures = sum(1 for m in equivalence["messages"] if not m["successful"])
        if equivalence["successful"]:
            text = f"✓ {equivalence['summary']} · {len(equivalence['messages'])} code objects match"
        else:
            text = f"✗ {equivalence['summary']} · {failures} of {len(equivalence['messages'])} failing"
        if self.checking:
            text += " · checking…"
        elif self.dirty:
            text += " · unchecked changes (Ctrl+R to check)"
        summary = self.query_one("#summary", Static)
        summary.update(text)
        summary.set_class(equivalence["successful"], "ok")
        summary.set_class(not equivalence["successful"], "bad")

    def update_line_info(self):
        row = self.editor.cursor_location[0]
        line = self.editor.tracker.line_at(row)
        analysis = self.patch["analysis"]
        parts = []
        kind = self.editor.error_rows().get(row)
        if kind:
            parts.append(Text(KIND_TEXT.get(kind, KIND_TEXT["general"]), style=f"bold {KIND_COLOR[kind]}"))
        diff = analysis["line_diffs"].get(line) if line is not None else None
        if diff:
            parts.append(diff_table(diff[0], diff[1], self.removed, self.added))
        elif analysis["compile_error"]:
            parts.append(Text(analysis["compile_error"]))
        self.query_one("#line-info", Static).update(Group(*parts))

    @on(TextArea.Changed, "#source")
    def source_changed(self):
        self.quit_armed = False
        self.update_summary()

    @on(TextArea.SelectionChanged, "#source")
    def cursor_moved(self):
        self.update_line_info()

    def jump_to(self, line: int):
        row = min(self.editor.tracker.row(line), self.editor.document.line_count - 1)
        self.editor.cursor_location = (row, 0)
        self.show_editor()
        self.editor.scroll_cursor_visible(center=True)
        self.editor.focus()
        self.update_line_info()

    @on(OptionList.OptionSelected, "#messages")
    def message_selected(self, event: OptionList.OptionSelected):
        message = self.patch["analysis"]["equivalence"]["messages"][int(event.option_id[1:])]
        if message["line"] > 0:
            self.jump_to(message["line"])
            self.query_one(TabbedContent).active = "line"

    @on(DataTable.RowSelected, "#bytecode")
    def bytecode_selected(self, event: DataTable.RowSelected):
        i = int(event.row_key.value)
        for pane in ("original_bytecode", "reconstructed_bytecode"):
            targets = self.patch["analysis"][pane]["targets"]
            if i < len(targets) and targets[i]:
                self.jump_to(targets[i])
                return

    @on(OptionList.OptionSelected, "#history")
    def history_selected(self, event: OptionList.OptionSelected):
        if self.dirty:
            self.notify("Check or undo your changes before loading another patch version.", severity="warning")
            return
        self.show_patch(self.state.patches[int(event.option_id[1:])])

    def lock_if_solved(self):
        """Once a patch matches the original bytecode the source is final, so editing and checking stop"""
        if self.state.solved:
            self.editor.read_only = True
            self.refresh_bindings()

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action == "check" and self.state.solved:
            return None
        return True

    def action_check(self):
        if self.checking or self.state.solved:
            return
        self.checking = True
        self.update_summary()
        self.run_check(self.editor.text)

    @work(thread=True, exclusive=True)
    def run_check(self, source: str):
        try:
            patch = self.state.check(source)
        except PatchLocked:
            patch = self.state.solved
        self.call_from_thread(self.check_done, patch, source)

    def check_done(self, patch: dict, source: str):
        self.checking = False
        # keep anything typed while the check ran, the results still describe the checked source, unless the patch is final
        self.show_patch(patch, replace_source=patch["successful"])
        self.lock_if_solved()
        if self.editor.text != source and not patch["successful"]:
            self.editor.set_results(LineTracker(), {})
            self.notify("The source changed during the check, check again to refresh the results.", severity="warning")
        equivalence = patch["analysis"]["equivalence"]
        if equivalence["successful"]:
            self.notify("The patch compiles to the original bytecode.")
        else:
            self.notify(equivalence["summary"], severity="error")
            self.action_next_error(from_top=True)

    def action_save(self):
        try:
            path = self.state.save(self.editor.text)
        except OSError as e:
            self.notify(f"Could not save: {e}", severity="error")
            return
        self.saved_source = self.editor.text
        self.quit_armed = False
        self.notify(f"Saved to {path}")

    def error_rows_sorted(self) -> list[int]:
        return sorted(self.editor.error_rows())

    def action_next_error(self, from_top: bool = False):
        rows = self.error_rows_sorted()
        if not rows:
            self.notify("No errors to jump to.")
            return
        current = -1 if from_top else self.editor.cursor_location[0]
        self.goto_row(next((r for r in rows if r > current), rows[0]))

    def action_previous_error(self):
        rows = self.error_rows_sorted()
        if not rows:
            self.notify("No errors to jump to.")
            return
        current = self.editor.cursor_location[0]
        self.goto_row(next((r for r in reversed(rows) if r < current), rows[-1]))

    def goto_row(self, row: int):
        self.editor.cursor_location = (row, 0)
        self.show_editor()
        self.editor.scroll_cursor_visible(center=True)
        self.editor.focus()
        self.query_one(TabbedContent).active = "line"

    def action_show_tab(self, tab: str):
        self.show_side()
        self.query_one(TabbedContent).active = tab

    def action_focus_editor(self):
        self.show_editor()
        self.editor.focus()

    async def action_quit(self):
        if self.editor.text != self.saved_source and not self.quit_armed:
            self.quit_armed = True
            self.notify("The source has unsaved changes. Press Ctrl+S to save or Ctrl+Q again to quit.", severity="warning")
            return
        self.exit()
