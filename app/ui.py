"""Tkinter UI:与监控线程同生共死。"""
from __future__ import annotations

import queue
import tkinter as tk
from tkinter import scrolledtext, ttk

from .monitor import Monitor

LEVEL_COLORS = {
    "info": "#1a1a1a", "ok": "#0a7d32", "warn": "#a15c00", "error": "#c62828",
}


class UIBridge:
    """线程安全的事件桥:监控线程投递,UI 线程消费。"""

    def __init__(self):
        self.q: queue.Queue = queue.Queue()
        self.log_widget = None
        self.state_vars: dict = {}
        self._state_cache: dict = {}

    # ---- 由监控线程调用 ----
    def post_log(self, msg: str, level: str = "info") -> None:
        self.q.put(("log", (msg, level)))

    def post_state(self, **kw) -> None:
        self.q.put(("state", kw))

    def post_notify(self, msg: str) -> None:
        self.q.put(("notify", msg))

    # ---- 由 UI 线程调用 ----
    def drain(self) -> None:
        while True:
            try:
                kind, payload = self.q.get_nowait()
            except queue.Empty:
                return
            try:
                if kind == "log":
                    self._append_log(*payload)
                elif kind == "state":
                    self._apply_state(payload)
                elif kind == "notify":
                    self._notify(payload)
            except Exception as e:  # 单条失败不拖垮 UI 泵
                import sys
                if sys.stderr:
                    print(f"UI event error: {e!r}", file=sys.stderr)

    def _append_log(self, msg: str, level: str) -> None:
        import datetime
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        if self.log_widget is not None:
            self.log_widget.configure(state="normal")
            self.log_widget.insert("end", f"[{ts}] {msg}\n", level)
            self.log_widget.see("end")
            self.log_widget.configure(state="disabled")

    def _apply_state(self, kw: dict) -> None:
        self._state_cache.update(kw)

    def _notify(self, msg: str) -> None:
        # 状态栏红字提示即可,避免弹窗打断
        self._apply_state({"notice": msg})


class AppUI:
    def __init__(self, config, start_monitor: bool = True):
        self.cfg = config
        self.bridge = UIBridge()

        self.root = tk.Tk()
        self.root.title("GLM 用量自动重置")
        self.root.geometry("620x560")
        self.root.minsize(560, 480)
        try:
            self.root.tk.call("tk", "scaling", 1.2)
        except Exception:
            pass

        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        # pythonw 下 stderr 为 None,tkinter 默认异常上报会二次失败;改为写日志面板
        self.root.report_callback_exception = self._tk_exception

        self.monitor = Monitor(config, self.bridge)
        if start_monitor:
            self._start_monitor_and_loop()

    def _tk_exception(self, exc, val, tb):
        try:
            self.bridge._append_log(f"界面异常:{val}", "error")
        except Exception:
            pass

    def _start_monitor_and_loop(self) -> None:
        self.monitor.start()
        self._poll()
        self.root.mainloop()

    # -------------------------------------------------- widgets
    def _build(self) -> None:
        pad = {"padx": 10, "pady": 4}

        # ---- 状态区 ----
        status = ttk.LabelFrame(self.root, text="状态", padding=10)
        status.pack(fill="x", **pad)

        row1 = ttk.Frame(status)
        row1.pack(fill="x")
        self.var_status = tk.StringVar(value="启动中…")
        self.var_login = tk.StringVar(value="检查登录中…")
        self.var_notice = tk.StringVar(value="")
        ttk.Label(row1, textvariable=self.var_status, font=("", 11, "bold")).pack(side="left")
        ttk.Label(row1, text="   登录: ").pack(side="left")
        ttk.Label(row1, textvariable=self.var_login).pack(side="left")
        ttk.Label(row1, textvariable=self.var_notice, foreground="#c62828").pack(side="right")

        self.bar_5h = self._quota_row(status, "5小时额度")
        self.bar_week = self._quota_row(status, "周额度  ")

        row3 = ttk.Frame(status)
        row3.pack(fill="x", pady=(6, 0))
        self.var_cards = tk.StringVar(value="可用重置卡:5小时 — | 周 —")
        self.var_used = tk.StringVar(value="本轮已用卡:0/2")
        ttk.Label(row3, textvariable=self.var_cards).pack(side="left")
        ttk.Label(row3, textvariable=self.var_used).pack(side="right")

        # ---- 设置区 ----
        cfgframe = ttk.LabelFrame(self.root, text="设置(修改后点击保存)", padding=10)
        cfgframe.pack(fill="x", **pad)

        def spin(parent, var, lo, hi, inc, w):
            return ttk.Spinbox(parent, from_=lo, to=hi, increment=inc, width=w,
                               textvariable=var)

        r1 = ttk.Frame(cfgframe)
        r1.pack(fill="x")
        ttk.Label(r1, text="触发阈值:5小时").pack(side="left")
        self.sp_th5 = tk.DoubleVar(value=self.cfg.threshold_5h)
        spin(r1, self.sp_th5, 50, 100, 0.5, 6).pack(side="left", padx=(3, 10))
        ttk.Label(r1, text="%  周").pack(side="left")
        self.sp_thw = tk.DoubleVar(value=self.cfg.threshold_week)
        spin(r1, self.sp_thw, 50, 100, 0.5, 6).pack(side="left", padx=(3, 10))
        ttk.Label(r1, text="%").pack(side="left")

        r2 = ttk.Frame(cfgframe)
        r2.pack(fill="x", pady=(6, 0))
        ttk.Label(r2, text="最多用卡:5小时").pack(side="left")
        self.sp_mc5 = tk.IntVar(value=self.cfg.max_cards_5h)
        spin(r2, self.sp_mc5, 0, 9, 1, 4).pack(side="left", padx=(3, 10))
        ttk.Label(r2, text="张  周").pack(side="left")
        self.sp_mcw = tk.IntVar(value=self.cfg.max_cards_week)
        spin(r2, self.sp_mcw, 0, 9, 1, 4).pack(side="left", padx=(3, 10))
        ttk.Label(r2, text="张").pack(side="left")

        r3 = ttk.Frame(cfgframe)
        r3.pack(fill="x", pady=(6, 0))
        ttk.Label(r3, text="检查间隔:慢").pack(side="left")
        self.sp_islow = tk.IntVar(value=self.cfg.interval_slow)
        spin(r3, self.sp_islow, 10, 3600, 10, 6).pack(side="left", padx=(3, 8))
        ttk.Label(r3, text="秒 快").pack(side="left")
        self.sp_ifast = tk.IntVar(value=self.cfg.interval_fast)
        spin(r3, self.sp_ifast, 5, 3600, 5, 6).pack(side="left", padx=(3, 10))
        ttk.Label(r3, text="秒").pack(side="left")

        r4 = ttk.Frame(cfgframe)
        r4.pack(fill="x", pady=(6, 0))
        ttk.Label(r4, text="快检阈值:5小时>").pack(side="left")
        self.sp_f5 = tk.DoubleVar(value=self.cfg.fast_5h)
        spin(r4, self.sp_f5, 0, 100, 1, 6).pack(side="left", padx=(3, 10))
        ttk.Label(r4, text="%  周>").pack(side="left")
        self.sp_fw = tk.DoubleVar(value=self.cfg.fast_week)
        spin(r4, self.sp_fw, 0, 100, 1, 6).pack(side="left", padx=(3, 10))
        ttk.Label(r4, text="%  (达到即切换快检)").pack(side="left")

        r5 = ttk.Frame(cfgframe)
        r5.pack(fill="x", pady=(6, 0))
        self.ck_auto = tk.BooleanVar(value=self.cfg.auto_reset)
        self.ck_dry = tk.BooleanVar(value=self.cfg.dry_run)
        self.ck_headless = tk.BooleanVar(value=self.cfg.headless)
        ttk.Checkbutton(r5, text="自动重置", variable=self.ck_auto).pack(side="left")
        ttk.Checkbutton(r5, text="演练模式(不真正用卡)", variable=self.ck_dry).pack(side="left", padx=8)
        ttk.Checkbutton(r5, text="无头浏览器", variable=self.ck_headless).pack(side="left")
        ttk.Label(r5, text="(重启后生效)", foreground="#888").pack(side="right")

        r6 = ttk.Frame(cfgframe)
        r6.pack(fill="x", pady=(6, 0))
        ttk.Button(r6, text="立即检查", command=self._check_now).pack(side="left")
        self._pause_btn = ttk.Button(r6, text="暂停", command=self._toggle_pause)
        self._pause_btn.pack(side="left", padx=6)
        self.btn_login = ttk.Button(r6, text="打开登录页", command=self._open_login)
        self.btn_login.pack(side="left")
        ttk.Button(r6, text="保存设置", command=self._save).pack(side="right")

        # ---- 日志区 ----
        logframe = ttk.LabelFrame(self.root, text="日志", padding=6)
        logframe.pack(fill="both", expand=True, **pad)
        self.txt = scrolledtext.ScrolledText(logframe, height=10, state="disabled",
                                             font=("Consolas", 10))
        self.txt.pack(fill="both", expand=True)
        for lv, color in LEVEL_COLORS.items():
            self.txt.tag_configure(lv, foreground=color)
        self.bridge.log_widget = self.txt

    def _quota_row(self, parent, label: str):
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=2)
        ttk.Label(row, text=label, width=9).pack(side="left")
        var = tk.StringVar(value="—")
        bar = ttk.Progressbar(row, length=260, maximum=100)
        bar.pack(side="left", padx=6)
        ttk.Label(row, textvariable=var, width=8).pack(side="left")
        return bar, var

    # -------------------------------------------------- actions
    def _check_now(self) -> None:
        self.var_notice.set("")
        self.monitor.wake()

    def _toggle_pause(self) -> None:
        if self.monitor.paused:
            self.monitor.resume()
            self._pause_btn["text"] = "暂停"
            self.var_status.set("监控中")
        else:
            self.monitor.pause()
            self._pause_btn["text"] = "继续"
            self.var_status.set("已暂停")

    def _open_login(self) -> None:
        self.monitor.open_login()

    def _save(self) -> None:
        self.cfg.threshold_5h = float(self.sp_th5.get())
        self.cfg.threshold_week = float(self.sp_thw.get())
        self.cfg.max_cards_5h = int(self.sp_mc5.get())
        self.cfg.max_cards_week = int(self.sp_mcw.get())
        self.cfg.interval_slow = int(self.sp_islow.get())
        self.cfg.interval_fast = int(self.sp_ifast.get())
        self.cfg.fast_5h = float(self.sp_f5.get())
        self.cfg.fast_week = float(self.sp_fw.get())
        self.cfg.auto_reset = bool(self.ck_auto.get())
        self.cfg.dry_run = bool(self.ck_dry.get())
        self.cfg.headless = bool(self.ck_headless.get())
        self.cfg.validate()
        self.cfg.save()
        self.bridge.post_log("设置已保存(无头模式等重启后生效)", "ok")

    # -------------------------------------------------- pump
    def _poll(self):
        # 无论渲染是否异常,都必须重新注册下一轮,否则界面永久停更
        try:
            self.bridge.drain()
            self._render(self.bridge._state_cache)
        except Exception as e:
            try:
                self.bridge._append_log(f"界面刷新异常:{e}", "error")
            except Exception:
                pass
        finally:
            try:
                if self.root.winfo_exists():
                    self.root.after(500, self._poll)
            except Exception:
                pass

    def _render(self, st: dict) -> None:
        if "paused" in st:
            self.var_status.set("已暂停" if st["paused"] else "监控中")
        if "login" in st:
            self.var_login.set(str(st["login"]))
        elif "five_hour_pct" in st:
            self.var_login.set("已登录")
        if "notice" in st:
            self.var_notice.set(str(st["notice"]))
        if "five_hour_pct" in st:
            pct5 = float(str(st["five_hour_pct"]).rstrip("%")) if st["five_hour_pct"] != "—" else 0
            self.bar_5h[1].set(str(st["five_hour_pct"]))
            self.bar_5h[0]["value"] = pct5
        if "week_pct" in st:
            pwk = float(str(st["week_pct"]).rstrip("%")) if st["week_pct"] != "—" else 0
            self.bar_week[1].set(str(st["week_pct"]))
            self.bar_week[0]["value"] = pwk
        if "five_hour_cards" in st:
            def fmt(n):
                return "—" if n < 0 else f"{n} 张"
            self.var_cards.set(
                f"可用重置卡:5小时 {fmt(st.get('five_hour_cards', -1))} | 周 {fmt(st.get('week_cards', -1))}")
        if "cards_used_5h" in st:
            cool = f"   冷却 {st['cooldown']}s" if st.get("cooldown") else ""
            spd = st.get("speed", "")
            self.var_used.set(
                f"已用卡:5小时 {st.get('cards_used_5h', 0)}/{st.get('max_cards_5h', self.cfg.max_cards_5h)}"
                f" 周 {st.get('cards_used_week', 0)}/{st.get('max_cards_week', self.cfg.max_cards_week)}"
                + (f"   [{spd}]" if spd else "") + cool)

    # -------------------------------------------------- close
    def _on_close(self) -> None:
        self.cfg.save()
        self.monitor.stop()
        self.root.after(100, self._wait_stop)

    def _wait_stop(self) -> None:
        self.monitor.join(timeout=6)
        try:
            self.root.destroy()
        finally:
            # 确保进程退出(浏览器子进程可能拖住解释器)
            import os
            os._exit(0)
