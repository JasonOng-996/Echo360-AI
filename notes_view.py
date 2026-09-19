"""Local, read-only notes indexing and a small Tk Markdown renderer.

No browser, engine or AI imports: browsing notes cannot start paid analysis.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
import html
import os
from pathlib import Path
import re
import tkinter as tk
import unicodedata


@dataclass(frozen=True)
class Note:
    folder: Path
    course: str
    lecture: str
    summary: Path | None
    visual: Path | None
    summary_stamp: tuple[int, int] | None
    visual_stamp: tuple[int, int] | None
    ordinal: int = 0

    @property
    def key(self):
        return str(self.folder)


def readable_note(path):
    try:
        if path.is_symlink() or not path.is_file():
            return None
        stat = path.stat()
        if not stat.st_size or not path.read_text(encoding="utf-8-sig", errors="replace").strip():
            return None
        return stat.st_size, stat.st_mtime_ns
    except OSError:
        return None


def scan_notes(root: Path) -> list[Note]:
    """Index supported final outputs; skip intermediate and previous versions."""
    root = Path(root)
    if not root.is_dir():
        return []
    ignore = {"video_frames", "visual_analysis_parts", ".browser_profile", ".venv", "__pycache__"}
    items = []
    for directory, dirs, files in os.walk(root, followlinks=False):
        folder = Path(directory)
        dirs[:] = [d for d in dirs if d not in ignore and not (folder / d).is_symlink()
                   and not getattr(folder / d, "is_junction", lambda: False)()]
        if not {"summary.md", "transcript_summary.md", "visual_analysis.md"}.intersection(files):
            continue
        summary = None
        summary_stamp = None
        for name in ("summary.md", "transcript_summary.md"):
            stamp = readable_note(folder / name)
            if stamp is not None:
                summary, summary_stamp = folder / name, stamp
                break
        visual = folder / "visual_analysis.md"
        visual_stamp = readable_note(visual)
        if not summary and visual_stamp is None:
            continue
        items.append(Note(folder, folder.parent.name, folder.name, summary,
                          visual if visual_stamp else None, summary_stamp, visual_stamp))
        dirs[:] = []
    return number_notes(items)


def chronological_key(note):
    match = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?=$|[_\s])", note.lecture)
    try:
        day = date(*map(int, match.groups())) if match else date.max
    except ValueError:
        day = date.max
    return note.course.casefold(), day, note.lecture.casefold(), note.key


def number_notes(notes):
    counts = {}
    ordered = []
    for note in sorted(notes, key=chronological_key):
        course = note.course.casefold()
        counts[course] = counts.get(course, 0) + 1
        ordered.append(replace(note, ordinal=counts[course]))
    return ordered


def note_label(note):
    prefix = f"{note.ordinal}. " if note.ordinal else ""
    return f"{prefix}{note.course} · {note.lecture}"


def choose_note(notes, previous, current_key, follow_latest):
    """Follow changed summaries, while respecting a reader's manual selection."""
    if not notes:
        return None
    by_key = {note.key: note for note in notes}
    previous = {note.key: note for note in previous}
    if follow_latest:
        changed = [note for note in notes if note.summary and
                   (note.key not in previous or note.summary_stamp != previous[note.key].summary_stamp)]
        if changed:
            return max(changed, key=lambda n: (n.summary_stamp[1], n.lecture, n.key)).key
    if current_key in by_key:
        return current_key
    summaries = [note for note in notes if note.summary]
    return max(summaries or notes,
               key=lambda n: ((n.summary_stamp or n.visual_stamp or (0, 0))[1], n.lecture, n.key)).key


def inline_spans(text, base=()):
    """Render common emphasis without executing links, HTML or image content."""
    pattern = re.compile(r"(`+)(.+?)\1|\*\*(.+?)\*\*|__(.+?)__")
    start = 0
    for match in pattern.finditer(text):
        if match.start() > start:
            yield text[start:match.start()], base
        if match.group(2) is not None:
            yield match.group(2), (*base, "inline_code")
        else:
            tags = base if any(tag.startswith("heading") for tag in base) else (*base, "bold")
            yield match.group(3) or match.group(4), tags
        start = match.end()
    if start < len(text):
        yield text[start:], base


def markdown_spans(text):
    """Keep code/tables/formula source readable in the dependency-free viewer."""
    fence = None
    for line in text.splitlines(keepends=True):
        fence_match = re.match(r"^\s*(`{3,}|~{3,})(.*)$", line.rstrip("\r\n"))
        if fence is not None:
            if fence_match and fence_match[1][0] == fence[0] and len(fence_match[1]) >= len(fence) and not fence_match[2].strip():
                fence = None
                yield "\n", ()
            else:
                yield line, ("code",)
            continue
        if fence_match:
            fence = fence_match[1]
            language = fence_match[2].strip()
            if language:
                yield language + "\n", ("code_label",)
            continue
        heading = re.match(r"^(#{1,6})\s+(.+?)(?:\s+#+)?\s*$", line)
        if heading:
            yield from inline_spans(heading[2] + "\n", ("heading" + str(min(len(heading[1]), 3)),))
        elif line.lstrip().startswith("|"):
            yield line, ("table",)
        elif re.match(r"^\s{0,3}([-*_])(?:\s*\1){2,}\s*$", line):
            yield "────────────\n", ("muted",)
        elif re.match(r"^\s*>\s?", line):
            yield from inline_spans(re.sub(r"^\s*>\s?", "", line), ("quote",))
        else:
            bullet = re.match(r"^(\s*)[-+*]\s+(.*)", line.rstrip("\r\n"))
            if bullet:
                yield from inline_spans(bullet[1] + "• " + bullet[2] + "\n", ("bullet",))
            else:
                yield from inline_spans(line)


@dataclass(frozen=True)
class Table:
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    alignments: tuple[str, ...]


def split_table_row(line):
    """Split structural pipes; preserve escaped pipes and code-span contents."""
    cells, cell = [], []
    ticks = 0
    pipes = 0
    i = 0
    line = line.strip()
    while i < len(line):
        char = line[i]
        if char == "\\" and i + 1 < len(line):
            following = line[i + 1]
            cell.append(following if following == "|" else char + following)
            i += 2
            continue
        if char == "`":
            end = i + 1
            while end < len(line) and line[end] == "`":
                end += 1
            size = end - i
            if not ticks:
                ticks = size
            elif ticks == size:
                ticks = 0
            cell.append(line[i:end])
            i = end
            continue
        if char == "|" and not ticks:
            cells.append("".join(cell).strip())
            cell = []
            pipes += 1
        else:
            cell.append(char)
        i += 1
    if not pipes:
        return None
    cells.append("".join(cell).strip())
    if cells and not cells[0] and line.startswith("|"):
        cells.pop(0)
    if cells and not cells[-1] and line.endswith("|"):
        cells.pop()
    return cells


def table_header(first, second):
    headers = split_table_row(first)
    separators = split_table_row(second)
    if not headers or len(headers) < 2 or not separators or len(headers) != len(separators):
        return None
    if not all(re.fullmatch(r":?-+:?", value) for value in separators):
        return None
    alignments = tuple("center" if value.startswith(":") and value.endswith(":") else
                       "right" if value.endswith(":") else "left" for value in separators)
    return headers, alignments


def markdown_blocks(text):
    """Recognize real tables outside fenced code; leave other source intact."""
    lines = text.splitlines(keepends=True)
    pending = []
    fence = None
    i = 0
    while i < len(lines):
        line = lines[i]
        marker = re.match(r"^\s*(`{3,}|~{3,})(.*)$", line.rstrip("\r\n"))
        if fence is not None or marker:
            if marker:
                if fence is None:
                    fence = marker[1]
                elif marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                    fence = None
            pending.append(line)
            i += 1
            continue
        header = table_header(line, lines[i + 1]) if i + 1 < len(lines) else None
        if header is None:
            pending.append(line)
            i += 1
            continue
        if pending:
            yield "".join(pending)
            pending.clear()
        headers, alignments = header
        rows = []
        i += 2
        while i < len(lines):
            row = split_table_row(lines[i])
            if row is None or not lines[i].strip():
                break
            if i + 1 < len(lines) and table_header(lines[i], lines[i + 1]):
                break
            if len(row) > len(headers):
                # Keep all text when a generated row has an unexpected extra pipe.
                row = row[:len(headers)-1] + [" | ".join(row[len(headers)-1:])]
            row += [""] * (len(headers) - len(row))
            rows.append(tuple(row))
            i += 1
        yield Table(tuple(headers), tuple(rows), alignments)
    if pending:
        yield "".join(pending)


def table_cell_text(value):
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
    return html.unescape("".join(part for part, tags in inline_spans(value)))


def table_column_widths(table, available):
    count = len(table.headers)
    minimum = 60
    available = max(available, count * minimum)
    weights = []
    for column in range(count):
        values = [table.headers[column], *(row[column] for row in table.rows)]
        lengths = [sum(2 if unicodedata.east_asian_width(c) in {"W", "F"} else 1
                       for c in table_cell_text(value)) for value in values]
        weights.append(max(8, min(36, max(lengths))))
    extra = available - count * minimum
    widths = [minimum + int(extra * weight / sum(weights)) for weight in weights]
    widths[-1] += available - sum(widths)
    return widths


def _reader_width(widget):
    return max(240, widget.winfo_width() - 2 * int(widget.cget("padx")) - 12)


def _scroll_table(widget, event):
    if getattr(event, "num", None) in {4, 5}:
        units = -3 if event.num == 4 else 3
    else:
        delta = getattr(event, "delta", 0)
        units = -int(delta / 120) if abs(delta) >= 120 else (-1 if delta > 0 else 1)
    widget.yview_scroll(units, "units")
    return "break"


class TableFrame(tk.Frame):
    """A resizable grid of wrapped cells embedded in the read-only Text widget."""
    def __init__(self, reader, table):
        super().__init__(reader, background="#cbd5e1", borderwidth=1)
        self.table = table
        self.cells = []
        self.previous_width = None
        body_font = reader.cget("font")
        for row_index, row in enumerate((table.headers, *table.rows)):
            for column, value in enumerate(row):
                alignment = table.alignments[column]
                bg = "#e7eef8" if row_index == 0 else ("#ffffff" if row_index % 2 else "#f4f7fb")
                label = tk.Label(self, text=table_cell_text(value), anchor={"left": "nw", "center": "n", "right": "ne"}[alignment],
                                 justify=alignment, font=("Microsoft YaHei UI", 11, "bold") if row_index == 0 else body_font,
                                 background=bg, foreground="#202c39", padx=7, pady=7, borderwidth=0)
                label.grid(row=row_index, column=column, sticky="nsew", padx=(0, 1), pady=(0, 1))
                self.cells.append((column, label))
                for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                    label.bind(sequence, lambda event: _scroll_table(reader, event))
        self.resize(_reader_width(reader))

    def resize(self, width):
        if width == self.previous_width:
            return
        self.previous_width = width
        widths = table_column_widths(self.table, width - 2)
        for column, size in enumerate(widths):
            self.columnconfigure(column, minsize=size, weight=1)
        for column, cell in self.cells:
            cell.configure(wraplength=max(30, widths[column] - 18))


def _resize_reader_tables(widget):
    widget._table_resize_job = None
    if widget.winfo_exists():
        for table in widget._markdown_tables:
            table.resize(_reader_width(widget))


def configure_reader(widget):
    widget._markdown_tables = []
    widget._table_resize_job = None
    def on_resize(event):
        if widget._table_resize_job is None:
            widget._table_resize_job = widget.after_idle(lambda: _resize_reader_tables(widget))
    widget.bind("<Configure>", on_resize, add="+")
    widget.configure(font=("Segoe UI", 11), background="#ffffff", foreground="#202c39",
                     padx=16, pady=14, spacing1=2, spacing3=5)
    widget.tag_configure("heading1", font=("Segoe UI", 19, "bold"), foreground="#16324f", spacing1=12, spacing3=8)
    widget.tag_configure("heading2", font=("Segoe UI", 15, "bold"), foreground="#16324f", spacing1=10, spacing3=6)
    widget.tag_configure("heading3", font=("Segoe UI", 12, "bold"), spacing1=8, spacing3=5)
    widget.tag_configure("bold", font=("Segoe UI", 11, "bold"))
    widget.tag_configure("code", font=("Consolas", 10), background="#f1f4f8", lmargin1=12, lmargin2=12, spacing1=0, spacing3=0)
    widget.tag_configure("inline_code", font=("Consolas", 10), background="#f1f4f8")
    widget.tag_configure("code_label", font=("Segoe UI", 9), foreground="#52677b", spacing1=6, spacing3=2)
    widget.tag_configure("table", font=("Consolas", 10), background="#f7f9fc", spacing1=0, spacing3=0)
    widget.tag_configure("quote", foreground="#52677b", lmargin1=16, lmargin2=16)
    widget.tag_configure("bullet", lmargin1=8, lmargin2=22)
    widget.tag_configure("muted", foreground="#8c9baa")


def render_markdown(widget, text):
    widget.configure(state="normal")
    try:
        for table in getattr(widget, "_markdown_tables", []):
            table.destroy()
        widget._markdown_tables = []
        widget.delete("1.0", "end")
        for block in markdown_blocks(text):
            if isinstance(block, Table):
                table = TableFrame(widget, block)
                widget._markdown_tables.append(table)
                widget.window_create("end", window=table, align="top", pady=8)
                widget.insert("end", "\n")
            else:
                for value, tags in markdown_spans(block):
                    widget.insert("end", value, tags)
        widget.yview_moveto(0)
    finally:
        widget.configure(state="disabled")
