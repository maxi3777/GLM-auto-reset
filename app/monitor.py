"""监控循环:轮询额度 -> 决策 -> (受预算/冷却约束地)使用重置卡。"""
from __future__ import annotations

import threading
import time
import traceback
import uuid
from datetime import datetime

from . import strategy
from .bigmodel import ApiError, BigModelClient
from .browser import BrowserClosed, BrowserController, CaptchaRequired, LoginRequired

CARD_UNAVAILABLE_MSG = "指定的重置次数不可用"


class Monitor(threading.Thread):
    def __init__(self, config, ui):
        super().__init__(daemon=True)
        self.cfg = config
        self.ui = ui                      # UI 桥(线程安全投递)
        self._stop = threading.Event()
        self._wake = threading.Event()    # 立即检查
        self._pause = threading.Event()
        self._force_headful = threading.Event()

        self.browser: BrowserController | None = None
        self.client: BigModelClient | None = None

        # 运行期状态
        self.cards_used = {strategy.FIVE_HOUR: 0, strategy.WEEK: 0}  # 本轮已用卡数(按卡类型)
        self.cooldown_until = 0.0
        self.login_notified = False
        self.consecutive_failures = 0
        self._interval_now = config.interval_slow   # 当前生效的轮询间隔
        self._last_blocked = ""                      # 上一个 blocked 原因(去重用)

    # -------------------------------------------------- control
    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def wake(self) -> None:
        self._wake.set()

    def pause(self) -> None:
        self._pause.set()

    def resume(self) -> None:
        self._pause.clear()
        self.wake()

    def open_login(self) -> None:
        self._force_headful.set()
        self.wake()

    @property
    def paused(self) -> bool:
        return self._pause.is_set()

    # -------------------------------------------------- helpers
    def log(self, msg: str, level: str = "info") -> None:
        self.ui.post_log(msg, level)

    def now(self) -> str:
        return datetime.now().strftime("%H:%M:%S")

    # -------------------------------------------------- main loop
    def run(self) -> None:
        self.log("监控线程已启动")
        try:
            while not self._stop.is_set():
                if self._pause.is_set():
                    self.ui.post_state(paused=True)
                    self._wake.wait(1.0)
                    self._wake.clear()
                    continue
                try:
                    self._cycle()
                    self.consecutive_failures = 0
                except LoginRequired:
                    self._handle_login()
                except CaptchaRequired:
                    self._handle_captcha()
                except BrowserClosed:
                    self.log("浏览器已关闭,3 秒后重启", "warn")
                    self._sleep(3)
                    self.browser = None
                except Exception as e:
                    self.consecutive_failures += 1
                    self.log(f"检查异常:{e}", "error")
                    traceback.print_exc()
                    if self.consecutive_failures >= 3 and self.browser and self.browser.headless:
                        self.browser.escalate_headful("连续失败,切换有头模式排查")
                        self.consecutive_failures = 0
                    self._sleep(min(30 * self.consecutive_failures, 120))
                    continue
                # 等待下一轮(可被 wake 提前打断);间隔按快/慢检查自动切换
                self._wake.wait(self._interval_now)
                self._wake.clear()
        finally:
            self._cleanup()

    def _sleep(self, sec: float) -> None:
        self._wake.wait(sec)
        self._wake.clear()

    # -------------------------------------------------- one cycle
    def _cycle(self) -> None:
        self._ensure_browser()

        if self._force_headful.is_set():
            self._force_headful.clear()
            if self.browser.headless:
                self.browser.escalate_headful("用户请求打开登录页")
            else:
                self.browser.goto_usage()

        if not self.browser.check_login():
            raise LoginRequired("被重定向到登录页")

        # 真实站点未登录时 SPA 可能不跳转 /login,以令牌 cookie 为准
        if "bigmodel.cn" in self.browser.current_url() and not self.browser.has_token():
            raise LoginRequired("缺少登录令牌")

        if self.browser.detect_captcha():
            raise CaptchaRequired("页面出现安全验证")

        # 1. 额度
        quota_raw = self.client.get_quota_raw()
        snap = strategy.parse_quota(quota_raw)
        if snap.five_hour is None and snap.week is None:
            raise RuntimeError(f"额度数据无法解析:{str(quota_raw)[:200]}")

        # 2. 重置卡列表
        try:
            cards_raw = self.client.get_cards_raw()
            snap.cards = strategy.parse_cards(cards_raw)
        except Exception as e:
            self.log(f"重置卡列表获取失败:{e}", "warn")

        # 3. 双速轮询:任一额度达到快检阈值 -> 快间隔
        fast = strategy.fast_check_mode(snap, self.cfg.fast_5h, self.cfg.fast_week)
        self._interval_now = self.cfg.interval_fast if fast else self.cfg.interval_slow

        # 4. 上报 UI
        fh_txt = f"{snap.five_hour.percent:.1f}%" if snap.five_hour else "—"
        wk_txt = f"{snap.week.percent:.1f}%" if snap.week else "—"
        fh_cards = len(snap.cards.available(strategy.FIVE_HOUR)) if snap.cards else -1
        wk_cards = len(snap.cards.available(strategy.WEEK)) if snap.cards else -1
        self.ui.post_state(
            five_hour_pct=fh_txt, week_pct=wk_txt,
            five_hour_cards=fh_cards, week_cards=wk_cards,
            cards_used_5h=self.cards_used[strategy.FIVE_HOUR],
            cards_used_week=self.cards_used[strategy.WEEK],
            max_cards_5h=self.cfg.max_cards_5h, max_cards_week=self.cfg.max_cards_week,
            threshold_5h=self.cfg.threshold_5h, threshold_week=self.cfg.threshold_week,
            speed="快检" if fast else "慢检",
            dry_run=self.cfg.dry_run, auto_reset=self.cfg.auto_reset,
        )

        # 5. 决策与动作
        if not self.cfg.auto_reset:
            return
        if time.time() < self.cooldown_until:
            remain = int(self.cooldown_until - time.time())
            self.ui.post_state(cooldown=remain)
            return

        remaining = {
            strategy.FIVE_HOUR: self.cfg.max_cards_5h - self.cards_used[strategy.FIVE_HOUR],
            strategy.WEEK: self.cfg.max_cards_week - self.cards_used[strategy.WEEK],
        }
        decision = strategy.decide(snap, self.cfg.threshold_5h, self.cfg.threshold_week,
                                   remaining, self.cfg.fallback_week_for_5h)
        if decision.action == "blocked":
            if decision.reason != self._last_blocked:
                self.log(decision.reason, "warn")
                self._last_blocked = decision.reason
            return
        if decision.action != "use_card":
            self._last_blocked = ""
            return
        self._last_blocked = ""

        card = decision.card
        label = f"{decision.reset_type} 卡 recordId={card.record_id} 有效期至 {card.expire_time}"
        if decision.is_fallback:
            label += "(兜底)"
        if self.cfg.dry_run:
            self.log(f"[演练] 额度已达阈值,本应使用 {label};演练模式未真正调用接口", "warn")
            self.cooldown_until = time.time() + 60
            return

        self._perform_use(decision, label)

    # -------------------------------------------------- actions
    def _perform_use(self, decision, label: str) -> None:
        request_id = str(uuid.uuid4())
        self.log(f"额度达到阈值,开始使用 {label}")
        try:
            resp = self.client.use_card(
                decision.reset_type, decision.card.record_id,
                decision.card.grant_type, request_id,
            )
        except LoginRequired:
            raise
        except Exception as e:
            # 结果未知:绝不盲目重试,先核对状态
            self.log(f"使用请求结果未知({e}),核对实际状态中…", "warn")
            self._resolve_ambiguous(decision, label)
            return

        if resp.get("code") == 200 and resp.get("success") is True:
            self._on_use_success(decision, label)
        else:
            code = resp.get("code")
            msg = resp.get("msg") or "未知错误"
            self.log(f"使用失败 [{code}] {msg}", "error")
            if CARD_UNAVAILABLE_MSG in msg:
                self.log("卡片状态已变化,稍后重新获取列表", "warn")
            self.cooldown_until = time.time() + 60

    def _resolve_ambiguous(self, decision, label: str) -> None:
        """POST 结果未知时:通过只读接口判断是否已生效,不做任何重试。"""
        try:
            cards = strategy.parse_cards(self.client.get_cards_raw())
            quota = strategy.parse_quota(self.client.get_quota_raw())
            card = next((c for c in (cards.five_hour + cards.week)
                         if c.record_id == decision.card.record_id), None)
            quota_dropped = (
                (decision.reset_type == strategy.WEEK and quota.week and quota.week.percent < self.cfg.threshold) or
                (decision.reset_type == strategy.FIVE_HOUR and quota.five_hour and quota.five_hour.percent < self.cfg.threshold)
            )
            if (card is not None and not card.available) or quota_dropped:
                self.log("核对结果:重置卡实际已生效", "warn")
                self._on_use_success(decision, label, quiet=True)
            else:
                self.log("核对结果:未生效。已停止自动重试,10 分钟后再评估", "error")
                self.cooldown_until = time.time() + 600
        except Exception as e:
            self.log(f"状态核对失败({e}),已进入长冷却,避免重复消耗", "error")
            self.cooldown_until = time.time() + 600

    def _on_use_success(self, decision, label: str, quiet: bool = False) -> None:
        kind_key = decision.reset_type
        self.cards_used[kind_key] += 1
        self.cooldown_until = time.time() + self.cfg.cooldown_sec
        kind = "5小时" if decision.reset_type == strategy.FIVE_HOUR else "周"
        used = self.cards_used[kind_key]
        limit = self.cfg.max_cards_5h if kind_key == strategy.FIVE_HOUR else self.cfg.max_cards_week
        self.log(f"✔ 已使用 {label}({kind}卡本轮第 {used}/{limit} 张)", "ok")
        self.ui.post_notify(f"已使用{kind}重置卡(第 {used}/{limit} 张)")
        # 复核额度变化
        try:
            quota = strategy.parse_quota(self.client.get_quota_raw())
            if decision.reset_type == strategy.WEEK and quota.week:
                self.log(f"复核:周额度已回落至 {quota.week.percent:.1f}%")
            elif decision.reset_type == strategy.FIVE_HOUR and quota.five_hour:
                self.log(f"复核:5小时额度已回落至 {quota.five_hour.percent:.1f}%")
        except Exception:
            pass

    # -------------------------------------------------- login / captcha
    def _ensure_browser(self) -> None:
        if self.browser is None:
            def on_event(kind, msg):
                if kind == "log":
                    self.log(msg)
            self.browser = BrowserController(
                self.cfg.profile_dir, self.cfg.usage_url, on_event=on_event)
            self.browser.start(headless=self.cfg.headless)
            self.client = BigModelClient(self.browser)
            self.browser.goto_usage()

    def _handle_login(self) -> None:
        if not self.login_notified:
            self.log("需要登录:浏览器窗口已打开,请完成登录(登录态会自动保存)", "warn")
            self.ui.post_state(login="需要登录")
            self.ui.post_notify("GLM 用量监控:请在浏览器窗口中完成登录")
            self.login_notified = True
        if self.browser.headless:
            self.browser.escalate_headful("需要登录")
            self.login_notified = False  # 有头打开后再提示一次
        else:
            self.browser.goto_usage()
        self.ui.post_state(login="等待登录…")
        self._sleep(5)

    def _handle_captcha(self) -> None:
        self.log("页面出现安全验证,请在浏览器窗口中手动完成验证", "warn")
        self.ui.post_state(login="等待安全验证")
        self.ui.post_notify("GLM 用量监控:请在浏览器中完成安全验证")
        if self.browser.headless:
            self.browser.escalate_headful("安全验证")
        self._sleep(8)

    # -------------------------------------------------- cleanup
    def _cleanup(self) -> None:
        self.log("正在关闭浏览器…")
        if self.browser is not None:
            try:
                self.browser.close()
            except Exception:
                pass
        self.log("监控已停止")
