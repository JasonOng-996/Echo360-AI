from __future__ import annotations

import asyncio
import contextlib
from copy import deepcopy
import os
from pathlib import Path
import queue
import sys
import subprocess
import threading
import traceback
import webbrowser
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import uq_echo_ai as engine
from runtime_support import TaskControl, TaskStopped, redact, atomic_text
from notes_view import scan_notes, choose_note, configure_reader, render_markdown, note_label

APP_TITLE = "EchoLecture AI"
APP_VERSION = "2.2.1"


class QueueWriter:
    def __init__(self, events):
        self.events = events
        self.pending = ""

    def write(self, text):
        self.pending += text
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            self.events.put(("log", redact(line) + "\n"))
        return len(text)

    def flush(self):
        if self.pending:
            self.events.put(("log", redact(self.pending)))
            self.pending = ""


class EchoLectureApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_TITLE} {APP_VERSION}")
        self.geometry("1280x820")
        self.minsize(1040, 720)
        self.events = queue.Queue()
        self.worker = None
        self.control = None
        self.input_request = None
        self.pending_request = None
        self.selected_course_ids = set()
        self.closing = False
        self.notes = []
        self.current_note_key = None
        self.note_rendered = {}
        self.note_raw = {}
        self.notes_scan_running = False
        self.notes_rescan_requested = False
        self.reader_fullscreen = False
        self.reader_saved_layout = None
        self.settings_controls = []
        self.saved_states = []
        self.cfg = engine.load_config()
        self._build_ui()
        self._load_settings()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<F11>", self.toggle_reader_fullscreen)
        self.bind("<Escape>", self.exit_reader_fullscreen)
        self.after_idle(self._initial_reader_split)
        self.after(100, self._poll_events)
        self.after(150, self.refresh_notes)
        self.after(4000, self._watch_notes)

    def _build_ui(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        top = ttk.Frame(self, padding=(16, 12))
        self.header_frame = top
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(0, weight=1)
        ttk.Label(top, text=f"EchoLecture AI  {APP_VERSION}", font=("Segoe UI", 19, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(top, text="课程录像 · 字幕 · 画面分析 · 学习笔记").grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.import_btn = ttk.Button(top, text="导入旧版设置", command=self.import_settings)
        self.import_btn.grid(row=0, column=1, rowspan=2, padx=6)
        self.settings_controls.append(self.import_btn)
        ttk.Button(top, text="打开设置目录", command=lambda: self.open_folder(engine.APP_HOME)).grid(row=0, column=2, rowspan=2)

        panes = ttk.Panedwindow(self, orient="horizontal")
        self.panes = panes
        panes.grid(row=1, column=0, sticky="nsew", padx=16, pady=(0, 10))
        left = ttk.Frame(panes, padding=(0, 0, 12, 0))
        right = ttk.Frame(panes, padding=(10, 0, 0, 0))
        self.left_pane = left
        self.right_pane = right
        panes.add(left, weight=0)
        panes.add(right, weight=1)
        left.columnconfigure(0, weight=1)
        left.rowconfigure(0, weight=1)
        notebook = ttk.Notebook(left)
        notebook.grid(row=0, column=0, sticky="nsew")
        course = ttk.Frame(notebook, padding=12)
        ai = ttk.Frame(notebook, padding=12)
        notebook.add(course, text="课程与文件")
        notebook.add(ai, text="AI 与抽帧")
        self.vars = {}

        def field(parent, row, label, name, values=None, secret=False):
            parent.columnconfigure(1, weight=1)
            ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=6)
            variable = tk.StringVar()
            self.vars[name] = variable
            if values:
                widget = ttk.Combobox(parent, textvariable=variable, values=values, state="readonly", width=22)
            else:
                widget = ttk.Entry(parent, textvariable=variable, width=24, show="•" if secret else "")
            widget.grid(row=row, column=1, sticky="ew", padx=(8, 0), pady=6)
            self.settings_controls.append(widget)
            return widget

        field(course, 0, "学校登录网址", "login_url")
        field(course, 1, "Echo360 Courses", "courses_url")
        field(course, 2, "学期，例如 2026S2", "term_filter")
        field(course, 3, "保存目录", "output_root")
        browse = ttk.Button(course, text="选择保存目录…", command=self.browse_output)
        browse.grid(row=4, column=1, sticky="e")
        self.settings_controls.append(browse)
        field(course, 5, "课程选择", "course_selection", ["all", "prompt"])
        field(course, 6, "每门课最多处理（0=全部）", "max_lectures_per_run")
        self.flags = {}
        for row, (name, label) in enumerate([
            ("download_video", "下载 Lower MP4"), ("download_vtt", "下载 VTT 字幕"),
            ("analyze_video", "分析 MP4 关键画面"), ("summarize_with_api", "生成 summary.md 学习笔记"),
            ("only_missing", "仅补缺失文件（保留旧版分析）")], 7):
            var = tk.BooleanVar()
            self.flags[name] = var
            check = ttk.Checkbutton(course, text=label, variable=var)
            check.grid(row=row, column=0, columnspan=2, sticky="w", pady=5)
            self.settings_controls.append(check)
        ttk.Label(course, text="在 Edge 完成学校登录和 MFA，进入 Lecture Recordings。\n登录确认和课程选择都在右侧“任务与日志”内操作。",
                  wraplength=420, justify="left").grid(row=12, column=0, columnspan=2, sticky="w", pady=(10, 0))
        ttk.Label(course, text="可先检查缺失文件，不调用 API。勾选“仅补缺失”时，已有非空笔记和画面分析直接保留。",
                  wraplength=420).grid(row=13, column=0, columnspan=2, sticky="w", pady=(8, 0))

        provider = field(ai, 0, "AI 服务", "ai_provider", ["openai", "anthropic"])
        provider.bind("<<ComboboxSelected>>", lambda _: self._provider_changed())
        field(ai, 1, "总结模型 ID", "model")
        field(ai, 2, "视觉模型 ID", "vision_model")
        self.key_entry = field(ai, 3, "API Key（本机保存）", "api_key", secret=True)
        self.workspace_entry = field(ai, 4, "Claude Workspace ID", "workspace_id")
        field(ai, 5, "总帧数上限（0=不限）", "video_max_frames")
        field(ai, 6, "最小帧间隔（秒）", "video_min_frame_gap_sec")
        field(ai, 7, "画面扫描间隔（秒）", "video_scene_scan_interval_sec")
        field(ai, 8, "覆盖间隔（秒）", "video_coverage_interval_sec")
        ttk.Label(ai, text="模型 ID 可直接输入。请先测试当前账户是否能调用该模型。\nAPI 按服务商规则计费；分析会发送字幕和抽取的画面。",
                  wraplength=410, justify="left").grid(row=9, column=0, columnspan=2, sticky="w", pady=(12, 0))
        ttk.Label(ai, text="默认保留旧版已有分析，不因模型、抽帧参数或缓存变化重做。取消“仅补缺失文件”后，缓存不匹配的内容可能重新调用 API。",
                  wraplength=410).grid(row=10, column=0, columnspan=2, sticky="w", pady=(12, 0))

        actions = ttk.Frame(left, padding=(0, 10, 0, 0))
        actions.grid(row=1, column=0, sticky="ew")
        for column in range(2):
            actions.columnconfigure(column, weight=1)
        action_defs = [("保存设置", self.save_settings, 0, 0), ("测试 API", self.test_api, 0, 1),
                       ("下载并分析课程", self.start_courses, 1, 0), ("分析已下载文件", self.analyze_existing, 1, 1)]
        for label, command, row, column in action_defs:
            button = ttk.Button(actions, text=label, command=command)
            button.grid(row=row, column=column, sticky="ew", padx=3, pady=3)
            self.settings_controls.append(button)
        inspect_btn = ttk.Button(actions, text="检查缺失文件（不调用 API）", command=self.inspect_missing)
        inspect_btn.grid(row=2, column=0, columnspan=2, sticky="ew", padx=3, pady=3)
        self.settings_controls.append(inspect_btn)
        ttk.Button(actions, text="查看登录 / 下载进度", command=lambda: self.right_tabs.select(self.task_page)).grid(row=3, column=0, sticky="ew", padx=3, pady=3)
        self.stop_btn = ttk.Button(actions, text="停止任务", command=self.stop_task, state="disabled")
        self.stop_btn.grid(row=3, column=1, sticky="ew", padx=3, pady=3)

        right.columnconfigure(0, weight=1)
        right.rowconfigure(2, weight=1)
        self.status = tk.StringVar(value="就绪")
        self.status_label = ttk.Label(right, textvariable=self.status, font=("Segoe UI", 11, "bold"), wraplength=680)
        self.status_label.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        self.progress = ttk.Progressbar(right, mode="determinate")
        self.progress.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        self.right_tabs = ttk.Notebook(right)
        self.right_tabs.bind("<<NotebookTabChanged>>", self._right_tab_changed)
        self.right_tabs.grid(row=2, column=0, columnspan=2, sticky="nsew")
        self.notes_page = ttk.Frame(self.right_tabs, padding=8)
        self.task_page = ttk.Frame(self.right_tabs, padding=8)
        self.right_tabs.add(self.notes_page, text="AI 总结")
        self.right_tabs.add(self.task_page, text="任务与日志")
        self._build_notes_reader()
        self._build_task_panel()
        self.task_page.columnconfigure(0, weight=1)
        self.task_page.rowconfigure(2, weight=1)
        ttk.Label(self.task_page, text="运行日志").grid(row=1, column=0, sticky="w", pady=(10, 4))
        self.log = tk.Text(self.task_page, wrap="word", font=("Consolas", 10), state="disabled", width=40, height=10)
        self.log.grid(row=2, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(self.task_page, orient="vertical", command=self.log.yview)
        scroll.grid(row=2, column=1, sticky="ns")
        self.log.configure(yscrollcommand=scroll.set)
        bottom = ttk.Frame(right)
        self.right_footer = bottom
        bottom.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Button(bottom, text="打开输出目录", command=self.open_output).pack(side="left")
        ttk.Button(bottom, text="导出日志…", command=self.export_log).pack(side="right")
        self.footer_label = ttk.Label(self, text="登录由本人完成；仅处理账户允许下载的内容。停止时会保留已完成结果。", padding=(16, 6))
        self.footer_label.grid(row=2, column=0, sticky="w")

    def _initial_reader_split(self):
        if not self.reader_fullscreen and len(self.panes.panes()) == 2:
            width = self.panes.winfo_width()
            if width > 800:
                self.panes.sashpos(0, max(360, min(460, int(width * 0.34))))

    def toggle_reader_fullscreen(self, event=None):
        self._set_reader_fullscreen(not self.reader_fullscreen)
        return "break"

    def exit_reader_fullscreen(self, event=None):
        if self.reader_fullscreen:
            self._set_reader_fullscreen(False)
            return "break"

    def _right_tab_changed(self, event=None):
        if self.reader_fullscreen and self.right_tabs.select() == str(self.task_page):
            self.exit_reader_fullscreen()

    def _set_reader_fullscreen(self, enabled):
        if enabled == self.reader_fullscreen:
            return
        if enabled:
            saved = {"state": self.state(), "geometry": self.geometry(), "sash": self.panes.sashpos(0)}
        else:
            saved = self.reader_saved_layout
        try:
            self.attributes("-fullscreen", enabled)
        except tk.TclError as exc:
            self.status.set("暂时无法切换全屏，请使用窗口最大化。")
            self._append(f"[READER] {redact(exc)}\n")
            return
        self.reader_fullscreen = enabled
        hidden = (self.header_frame, self.footer_label, self.status_label, self.progress, self.right_footer)
        if enabled:
            self.reader_saved_layout = saved
            self.right_tabs.select(self.notes_page)
            self.panes.forget(self.left_pane)
            for widget in hidden:
                widget.grid_remove()
            self.panes.grid_configure(padx=0, pady=0)
            self.right_pane.configure(padding=0)
            self.notes_page.configure(padding=4)
            self.fullscreen_btn.configure(text="退出全屏（Esc）")
        else:
            self.panes.insert(0, self.left_pane, weight=0)
            for widget in hidden:
                widget.grid()
            self.panes.grid_configure(padx=16, pady=(0, 10))
            self.right_pane.configure(padding=(10, 0, 0, 0))
            self.notes_page.configure(padding=8)
            self.fullscreen_btn.configure(text="全屏阅读（F11）")
            if saved["state"] == "normal":
                self.state("normal")
                self.geometry(saved["geometry"])
            elif saved["state"] == "zoomed":
                try:
                    self.state("zoomed")
                except tk.TclError:
                    self.geometry(saved["geometry"])
            def restore_split():
                if not self.reader_fullscreen and len(self.panes.panes()) == 2:
                    self.panes.sashpos(0, saved["sash"])
            self.after_idle(restore_split)

    def _build_task_panel(self):
        self.request_frame = ttk.LabelFrame(self.task_page, text="登录与课程", padding=10)
        self.request_frame.grid(row=0, column=0, columnspan=2, sticky="ew")
        self.request_frame.columnconfigure(0, weight=1)
        self.request_message = tk.StringVar(value="点击“下载并分析课程”开始。学校账号和 MFA 在 Edge 完成；确认和选课在这里继续。")
        ttk.Label(self.request_frame, textvariable=self.request_message, wraplength=530, justify="left").grid(row=0, column=0, sticky="ew")
        self.course_picker_frame = ttk.Frame(self.request_frame)
        self.course_picker_frame.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        self.course_picker_frame.columnconfigure(0, weight=1)
        self.course_picker = ttk.Treeview(self.course_picker_frame, columns=("title",), show="tree headings", height=4, selectmode="none")
        self.course_picker.heading("#0", text="选择课程")
        self.course_picker.column("#0", width=150, minwidth=100, stretch=False)
        self.course_picker.heading("title", text="课程名称")
        self.course_picker.column("title", width=290, minwidth=100)
        self.course_picker.grid(row=0, column=0, sticky="ew")
        scroll = ttk.Scrollbar(self.course_picker_frame, orient="vertical", command=self.course_picker.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.course_picker.configure(yscrollcommand=scroll.set)
        self.course_picker.bind("<Button-1>", self._toggle_course)
        self.course_picker.bind("<space>", self._toggle_course)
        selection_actions = ttk.Frame(self.course_picker_frame)
        selection_actions.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Button(selection_actions, text="全选", command=lambda: self._select_courses(True)).pack(side="left")
        ttk.Button(selection_actions, text="清空", command=lambda: self._select_courses(False)).pack(side="left", padx=6)
        self.course_picker_frame.grid_remove()
        self.request_text = tk.StringVar()
        self.request_entry = ttk.Entry(self.request_frame, textvariable=self.request_text)
        self.request_entry.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        self.request_entry.bind("<Return>", lambda event: self.continue_login())
        self.request_entry.grid_remove()
        self.continue_btn = ttk.Button(self.request_frame, text="登录完成，继续", command=self.continue_login, state="disabled")
        self.continue_btn.grid(row=3, column=0, sticky="e", pady=(8, 0))

    def _build_notes_reader(self):
        page = self.notes_page
        page.columnconfigure(1, weight=1)
        page.rowconfigure(4, weight=1)
        self.note_course = tk.StringVar(value="全部课程")
        self.note_lecture = tk.StringVar()
        ttk.Label(page, text="课程").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.note_course_box = ttk.Combobox(page, textvariable=self.note_course, state="readonly", width=26, values=["全部课程"])
        self.note_course_box.grid(row=0, column=1, sticky="ew", pady=3)
        self.note_course_box.bind("<<ComboboxSelected>>", self._note_course_changed)
        ttk.Label(page, text="课堂").grid(row=1, column=0, sticky="w", padx=(0, 8))
        self.note_lecture_box = ttk.Combobox(page, textvariable=self.note_lecture, state="readonly", width=26)
        self.note_lecture_box.grid(row=1, column=1, sticky="ew", pady=3)
        self.note_lecture_box.bind("<<ComboboxSelected>>", self._note_lecture_changed)
        toolbar = ttk.Frame(page)
        toolbar.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(4, 6))
        self.follow_latest = tk.BooleanVar(value=True)
        ttk.Checkbutton(toolbar, text="自动显示新总结", variable=self.follow_latest).pack(side="left")
        self.fullscreen_btn = ttk.Button(toolbar, text="全屏阅读（F11）", command=self.toggle_reader_fullscreen)
        self.fullscreen_btn.pack(side="right", padx=(6, 0))
        ttk.Button(toolbar, text="刷新", command=self.refresh_notes).pack(side="right")
        ttk.Button(toolbar, text="复制全文", command=self.copy_note).pack(side="right", padx=6)
        self.notes_hint = tk.StringVar(value="正在读取保存目录中的已有笔记…")
        ttk.Label(page, textvariable=self.notes_hint, wraplength=530, justify="left").grid(row=3, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        self.document_tabs = ttk.Notebook(page)
        self.document_tabs.grid(row=4, column=0, columnspan=2, sticky="nsew")
        self.note_widgets = {}
        self.document_pages = {}
        for key, label in (("summary", "学习笔记"), ("visual", "画面分析")):
            pane = ttk.Frame(self.document_tabs)
            pane.columnconfigure(0, weight=1)
            pane.rowconfigure(0, weight=1)
            self.document_tabs.add(pane, text=label)
            widget = tk.Text(pane, wrap="word", state="disabled", width=40, height=15)
            configure_reader(widget)
            widget.grid(row=0, column=0, sticky="nsew")
            scroll = ttk.Scrollbar(pane, orient="vertical", command=widget.yview)
            scroll.grid(row=0, column=1, sticky="ns")
            widget.configure(yscrollcommand=scroll.set)
            self.note_widgets[key] = widget
            self.document_pages[key] = pane

    def _load_settings(self):
        for name, var in self.vars.items():
            if name not in {"api_key", "workspace_id"}:
                var.set(str(self.cfg.get(name, "")))
        for name, var in self.flags.items():
            var.set(bool(self.cfg.get(name, True)))
        self._provider_changed()

    def _provider_changed(self):
        _, keys = engine.load_local_env(config=self.cfg)
        anthropic = self.vars["ai_provider"].get() == "anthropic"
        self.vars["api_key"].set(keys.get("ANTHROPIC_API_KEY" if anthropic else "OPENAI_API_KEY", ""))
        self.vars["workspace_id"].set(keys.get("ANTHROPIC_WORKSPACE_ID", ""))
        self.workspace_entry.configure(state="normal" if anthropic else "disabled")

    def save_settings(self, quiet=False):
        if self.worker and self.worker.is_alive():
            return False
        try:
            cfg = deepcopy(self.cfg)
            for name, var in self.vars.items():
                if name not in {"api_key", "workspace_id"}:
                    cfg[name] = var.get().strip()
            for name in ("video_max_frames", "video_min_frame_gap_sec", "video_scene_scan_interval_sec", "video_coverage_interval_sec", "max_lectures_per_run"):
                cfg[name] = int(cfg[name])
                minimum = 0 if name in {"video_max_frames", "max_lectures_per_run"} else 1
                if cfg[name] < minimum:
                    raise ValueError(f"{name} 必须至少为 {minimum}")
            for name, var in self.flags.items():
                cfg[name] = var.get()
            cfg["analyze_video_with_api"] = cfg["analyze_video"]
            if not cfg["output_root"]:
                raise ValueError("请选择保存目录")
            for name in ("login_url", "courses_url"):
                if cfg[name] and engine.urlparse(cfg[name]).scheme not in {"https", "http"}:
                    raise ValueError(f"{name} 需要完整的 https:// 网址")
            if not cfg["courses_url"]:
                cfg["courses_url"] = "https://echo360.net.au/courses"
            if not cfg["model"]:
                raise ValueError("请输入总结模型 ID")
            cfg["vision_model"] = cfg["vision_model"] or cfg["model"]
            engine.save_config(cfg)
            engine.save_credentials(cfg["ai_provider"], self.vars["api_key"].get(), self.vars["workspace_id"].get())
            root_changed = cfg.get("output_root") != self.cfg.get("output_root")
            self.cfg = cfg
            if root_changed:
                self.refresh_notes()
            if not quiet:
                self._append("[SETTINGS] 设置已保存。\n")
            return True
        except Exception as exc:
            self._report_error(str(exc))
            return False

    def _report_error(self, message):
        self.status.set(redact(message))
        self._append(f"[ERROR] {redact(message)}\n")
        self.right_tabs.select(self.task_page)

    def _set_busy(self, busy):
        if busy:
            self.saved_states = [(widget, str(widget.cget("state"))) for widget in self.settings_controls]
            for widget, _ in self.saved_states:
                widget.configure(state="disabled")
        else:
            for widget, state in self.saved_states:
                widget.configure(state=state)
            self.saved_states = []
        self.stop_btn.configure(state="normal" if busy else "disabled")

    def _run_worker(self, label, operation, uses_ai=False):
        if self.worker and self.worker.is_alive():
            return
        if not self.save_settings(quiet=True):
            return
        cfg = deepcopy(self.cfg)
        if uses_ai and (cfg["analyze_video"] or cfg["summarize_with_api"]) and not engine.selected_api_key(cfg):
            self._report_error("请填写 API Key，或取消勾选画面分析和生成总结以仅下载文件。")
            return
        control = TaskControl(progress_handler=lambda *args: self.events.put(("progress", *args)))
        def request(payload):
            responses = queue.Queue(maxsize=1)
            self.events.put(("request", payload, responses))
            while True:
                control.check()
                try:
                    return responses.get(timeout=0.1)
                except queue.Empty:
                    pass
        control.request_handler = request
        control.input_handler = lambda prompt: request({"kind": "text", "prompt": prompt, "options": []})
        self.control = control
        engine.CONTROL = control
        self._clear_request("任务正在进行，需要确认时会在这里显示。")
        self.right_tabs.select(self.task_page)
        self._set_busy(True)
        self.status.set(label)
        self.progress.configure(value=0)
        def target():
            writer = QueueWriter(self.events)
            outcome = "任务完成"
            try:
                with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
                    result = operation(cfg, control)
                    control.check()
                    if result is False:
                        outcome = "有未完成项目，请查看日志；重新运行可继续处理"
            except (TaskStopped, asyncio.CancelledError):
                outcome = "已停止；已完成结果已保留"
            except Exception:
                traceback.print_exc(file=writer)
                outcome = "任务失败，请查看日志"
            finally:
                writer.flush()
                self.events.put(("done", outcome))
        self.worker = threading.Thread(target=target, daemon=False)
        self.worker.start()

    def start_courses(self):
        self._run_worker("正在打开浏览器…", lambda cfg, ctl: asyncio.run(ctl.run(engine.batch_courses(cfg))), uses_ai=True)

    def analyze_existing(self):
        self._run_worker("正在扫描已下载文件…", lambda cfg, ctl: engine.analyze_existing_lectures(cfg), uses_ai=True)

    def inspect_missing(self):
        self._run_worker("正在检查缺失文件（不调用 API）…", lambda cfg, ctl: engine.inspect_missing_files(cfg))

    def test_api(self):
        self._run_worker("正在测试 API…", lambda cfg, ctl: engine.test_ai_api(cfg))

    def stop_task(self):
        if self.control:
            self.control.stop()
            self.stop_btn.configure(state="disabled")
            self._clear_request("已停止等待确认。已生成的笔记仍可在“AI 总结”中阅读。")
            self.status.set("正在停止…正在进行的 AI 请求结束后会退出，不再提交新批次。")

    def _poll_events(self):
        try:
            while True:
                kind, *payload = self.events.get_nowait()
                if kind == "log":
                    self._append(payload[0])
                elif kind == "progress":
                    label, current, total = payload
                    if self.control and not self.control.stopped.is_set():
                        self.status.set(label)
                        self.progress.configure(maximum=max(total, 1), value=min(current, max(total, 1)))
                elif kind == "request":
                    request, responses = payload
                    if self.control and self.control.stopped.is_set():
                        continue
                    self._show_request(request, responses)
                elif kind == "notes":
                    self._apply_notes(*payload)
                elif kind == "done":
                    self._set_busy(False)
                    self._clear_request("任务已结束。已有结果可在“AI 总结”中直接阅读。")
                    self.status.set(payload[0])
                    self._append("[RESULT] " + payload[0] + "\n")
                    if self.closing:
                        self.destroy()
                        return
                    self.refresh_notes()
        except queue.Empty:
            pass
        self.after(100, self._poll_events)

    def _clear_request(self, message=""):
        self.input_request = None
        self.pending_request = None
        self.selected_course_ids.clear()
        self.course_picker_frame.grid_remove()
        self.request_entry.grid_remove()
        self.continue_btn.configure(text="登录完成，继续", state="disabled")
        self.request_message.set(message)

    def _show_request(self, request, responses):
        self._clear_request(request["prompt"])
        self.input_request = responses
        self.pending_request = request
        kind = request["kind"]
        self.right_tabs.select(self.task_page)
        if kind == "courses":
            for row in self.course_picker.get_children():
                self.course_picker.delete(row)
            for option in request["options"]:
                self.course_picker.insert("", "end", iid=option["value"], text="☐ " + option["label"], values=(option.get("detail", ""),))
            self.course_picker_frame.grid()
            self.continue_btn.configure(text="下载所选课程", state="disabled")
            self.status.set("请在主界面勾选课程，再开始下载")
        elif kind == "login":
            self.continue_btn.configure(text="登录完成，继续", state="normal")
            self.status.set("等待学校登录完成；在下方继续")
        else:
            self.request_text.set("")
            self.request_entry.grid()
            self.request_entry.focus_set()
            self.continue_btn.configure(text="继续", state="normal")
            self.status.set("请在主界面填写所需信息")

    def _toggle_course(self, event):
        if not self.pending_request or self.pending_request["kind"] != "courses":
            return
        item = self.course_picker.focus() if getattr(event, "keysym", "") == "space" else self.course_picker.identify_row(event.y)
        if item:
            self.course_picker.focus(item)
            if item in self.selected_course_ids:
                self.selected_course_ids.remove(item)
            else:
                self.selected_course_ids.add(item)
            self._paint_course_selection()
        return "break"

    def _select_courses(self, select_all):
        if self.pending_request and self.pending_request["kind"] == "courses":
            self.selected_course_ids = {option["value"] for option in self.pending_request["options"]} if select_all else set()
            self._paint_course_selection()

    def _paint_course_selection(self):
        for option in self.pending_request["options"]:
            checked = option["value"] in self.selected_course_ids
            self.course_picker.item(option["value"], text=("☑ " if checked else "☐ ") + option["label"])
        count = len(self.selected_course_ids)
        self.continue_btn.configure(text=f"下载所选课程（{count} 门）", state="normal" if count else "disabled")

    def continue_login(self):
        if self.input_request is None or self.pending_request is None:
            return
        if self.control and self.control.stopped.is_set():
            self._clear_request("任务已停止。")
            return
        kind = self.pending_request["kind"]
        if kind == "courses":
            values = [o["value"] for o in self.pending_request["options"] if o["value"] in self.selected_course_ids]
            if not values:
                self.request_message.set("请至少勾选一门课程。")
                return
            answer = ",".join(values)
        else:
            answer = "" if kind == "login" else self.request_text.get().strip()
        try:
            self.input_request.put_nowait(answer)
        except queue.Full:
            return
        self._clear_request("已提交，正在继续处理…")
        self.status.set("正在扫描课程…" if kind == "login" else "正在处理所选课程…")

    def _watch_notes(self):
        if not self.closing:
            self.refresh_notes()
            self.after(4000, self._watch_notes)

    def refresh_notes(self):
        if self.closing:
            return
        if self.notes_scan_running:
            self.notes_rescan_requested = True
            return
        try:
            root = engine.expand_path(self.cfg.get("output_root", "~/Documents/EchoLectureAI"))
        except (OSError, ValueError) as exc:
            self.notes_hint.set(f"保存目录无效：{exc}")
            return
        self.notes_scan_running = True
        def scan():
            try:
                notes, error = scan_notes(root), ""
            except Exception as exc:
                notes, error = [], f"暂时无法读取笔记：{redact(exc)}"
            self.events.put(("notes", str(root), notes, error))
        threading.Thread(target=scan, daemon=True).start()

    def _apply_notes(self, root, notes, error):
        self.notes_scan_running = False
        expected = str(engine.expand_path(self.cfg.get("output_root", "~/Documents/EchoLectureAI")))
        if root != expected:
            self.notes_rescan_requested = False
            self.refresh_notes()
            return
        old = self.notes
        key = choose_note(notes, old, self.current_note_key, self.follow_latest.get())
        previous = {note.key: note for note in old}
        selected = next((note for note in notes if note.key == key), None)
        fresh_summary = bool(selected and selected.summary and
                             (key not in previous or selected.summary_stamp != previous[key].summary_stamp))
        self.notes = notes
        self.current_note_key = key
        courses = ["全部课程", *sorted({note.course for note in notes})]
        self.note_course_box.configure(values=courses)
        if self.note_course.get() not in courses or (self.follow_latest.get() and fresh_summary):
            self.note_course.set("全部课程")
        self._fill_note_lectures()
        selected = next((note for note in notes if note.key == self.current_note_key), None)
        fresh_summary = bool(selected and selected.summary and
                             (selected.key not in previous or selected.summary_stamp != previous[selected.key].summary_stamp))
        if selected is not None:
            self._display_note(selected)
            if fresh_summary and self.follow_latest.get() and self.pending_request is None:
                self.right_tabs.select(self.notes_page)
                self.document_tabs.select(self.document_pages["summary"])
        else:
            self.notes_hint.set(error or "保存目录中还没有可读笔记。已有笔记和新生成的结果会自动显示，阅读不会调用 API。")
            for kind, widget in self.note_widgets.items():
                if self.note_rendered.get(kind) != (None, None):
                    render_markdown(widget, "")
                self.note_rendered[kind] = (None, None)
                self.note_raw[kind] = ""
        if error:
            self.notes_hint.set(error)
        if self.notes_rescan_requested:
            self.notes_rescan_requested = False
            self.refresh_notes()

    def _fill_note_lectures(self):
        course = self.note_course.get()
        self.visible_notes = [note for note in self.notes if course == "全部课程" or note.course == course]
        self.note_lecture_box.configure(values=[note_label(note) for note in self.visible_notes])
        index = next((i for i, note in enumerate(self.visible_notes) if note.key == self.current_note_key), None)
        if index is None and self.visible_notes:
            index = 0
            self.current_note_key = self.visible_notes[0].key
        if index is not None:
            self.note_lecture_box.current(index)
        else:
            self.note_lecture.set("")

    def _note_course_changed(self, event=None):
        self.follow_latest.set(False)
        self._fill_note_lectures()
        self._note_lecture_changed()

    def _note_lecture_changed(self, event=None):
        index = self.note_lecture_box.current()
        if 0 <= index < len(self.visible_notes):
            self.follow_latest.set(False)
            note = self.visible_notes[index]
            self.current_note_key = note.key
            self._display_note(note)

    def _display_note(self, note):
        self.notes_hint.set(f"{note_label(note)}　|　本地阅读，不调用 API")
        for kind, path, stamp in (("summary", note.summary, note.summary_stamp), ("visual", note.visual, note.visual_stamp)):
            signature = (str(path) if path else None, stamp)
            if signature == self.note_rendered.get(kind):
                continue
            if path is None:
                text = "这节课还没有学习笔记。生成后会自动显示。" if kind == "summary" else "这节课还没有画面分析文件。"
            else:
                try:
                    text = path.read_text(encoding="utf-8-sig", errors="replace")
                except OSError as exc:
                    self.notes_hint.set(f"笔记正在更新或暂时无法读取：{redact(exc)}")
                    render_markdown(self.note_widgets[kind], "暂时无法读取这份文件，稍后将自动重试。")
                    self.note_rendered.pop(kind, None)
                    self.note_raw[kind] = ""
                    continue
            render_markdown(self.note_widgets[kind], text)
            self.note_rendered[kind] = signature
            self.note_raw[kind] = text if path else ""

    def copy_note(self):
        selected = self.document_tabs.select()
        kind = next((kind for kind, pane in self.document_pages.items() if str(pane) == selected), "summary")
        text = self.note_raw.get(kind, "")
        if text:
            self.clipboard_clear()
            self.clipboard_append(text)
            self.notes_hint.set("已复制当前内容。")

    def _append(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", redact(text))
        if int(self.log.index("end-1c").split(".")[0]) > 5000:
            self.log.delete("1.0", "1000.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def import_settings(self):
        directory = filedialog.askdirectory(title="选择旧版解压文件夹（会自动查找 config.json）", parent=self)
        if not directory:
            return
        try:
            try:
                source = engine.resolve_legacy_settings_source(directory)
            except engine.LegacyConfigSelectionNeeded as exc:
                self._append(f"[SETTINGS] {exc}\n")
                chosen = filedialog.askopenfilename(
                    title=str(exc), parent=self, initialdir=directory,
                    filetypes=[("旧版配置 config.json", "config.json"), ("JSON 配置文件", "*.json")])
                if not chosen:
                    return
                source = Path(chosen)
            self.cfg = engine.import_legacy_settings(source)
            self._load_settings()
            self._append(f"[SETTINGS] 已导入：{source}\n")
            self._append("[SETTINGS] 同目录中的可用 API Key 已导入；视频文件留在原目录。\n")
            self.refresh_notes()
        except Exception as exc:
            messagebox.showerror(APP_TITLE, redact(exc), parent=self)

    def browse_output(self):
        path = filedialog.askdirectory(parent=self)
        if path:
            self.vars["output_root"].set(path)

    def open_output(self):
        value = self.vars["output_root"].get().strip()
        if value:
            self.open_folder(engine.expand_path(value))

    @staticmethod
    def open_folder(path):
        path.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(str(path))
        else:
            webbrowser.open(path.as_uri())

    def export_log(self):
        path = filedialog.asksaveasfilename(parent=self, defaultextension=".txt", initialfile="EchoLectureAI-log.txt", filetypes=[("Text", "*.txt")])
        if path:
            atomic_text(Path(path), redact(self.log.get("1.0", "end-1c")))

    def _on_close(self):
        if self.worker and self.worker.is_alive():
            self.closing = True
            self.stop_task()
        else:
            self.destroy()


def main():
    try:
        app = EchoLectureApp()
        if "--self-test" in sys.argv:
            app.withdraw()
            app.update_idletasks()
            subprocess.run([engine.ffmpeg_exe(), "-version"], check=True, timeout=20,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            app.destroy()
            return
        app.mainloop()
    except Exception as exc:
        atomic_text(engine.APP_HOME / "startup-error.log", redact(traceback.format_exc()))
        if "--self-test" in sys.argv:
            raise SystemExit(1)
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(APP_TITLE, f"启动失败：{redact(exc)}\n详情：{engine.APP_HOME / 'startup-error.log'}", parent=root)
            root.destroy()
        except Exception:
            print(redact(exc))


if __name__ == "__main__":
    main()
