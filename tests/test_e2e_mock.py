"""端到端测试:真实 Playwright 浏览器 + 本地 mock 服务器,零接触真实网站。

直接驱动 Monitor._cycle()(不起线程),完全确定性。
"""
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest
from mock_server import MockServer

from app.browser import LoginRequired
from app.config import Config
from app.monitor import Monitor
from app import strategy

pytestmark = pytest.mark.e2e


class RecordingUI:
    def __init__(self):
        self.logs = []
        self.states = []
        self.notifies = []

    def post_log(self, msg, level="info"):
        self.logs.append((level, msg))

    def post_state(self, **kw):
        self.states.append(kw)

    def post_notify(self, msg):
        self.notifies.append(msg)

    def has_log(self, text):
        return any(text in m for _, m in self.logs)


@pytest.fixture(scope="module")
def server():
    s = MockServer()
    yield s
    s.stop()


@pytest.fixture
def cfg(server, tmp_path):
    def make(**kw):
        c = Config()
        c.threshold_5h = 97
        c.threshold_week = 97
        c.max_cards_5h = 2
        c.max_cards_week = 2
        c.interval_slow = 60
        c.interval_fast = 10
        c.fast_5h = 85
        c.fast_week = 95
        c.auto_reset = True
        c.dry_run = False
        c.headless = True
        c.usage_url = server.usage_url
        c.profile_dir = str(tmp_path / "profile")
        for k, v in kw.items():
            setattr(c, k, v)
        return c
    return make


def used(m: Monitor) -> int:
    return sum(m.cards_used.values())


def new_monitor(config) -> Monitor:
    m = Monitor(config, RecordingUI())
    return m


def run_cycles(m: Monitor, n: int = 1):
    m._ensure_browser()
    for _ in range(n):
        m._cycle()


class TestHappyPath:
    def test_below_threshold_no_consumption(self, server, cfg):
        server.state.set_percent(36.2, 83.0)
        server.state.reset_cards()
        m = new_monitor(cfg())
        try:
            run_cycles(m)
            assert server.state.used_count() == 0
            assert m.ui.has_log("额度未达阈值") is False  # no_action 不打日志也行
            assert used(m) == 0
        finally:
            m.browser.close()

    def test_5h_over_uses_priority_card(self, server, cfg):
        server.state.set_percent(98.4, 50.0)
        server.state.reset_cards()
        server.state.use_requests.clear()
        m = new_monitor(cfg())
        try:
            run_cycles(m)
            assert m.cards_used[strategy.FIVE_HOUR] == 1
            assert server.state.used_count() == 1
            # 优先卡 = 5h 可用卡中 expireTime 最早(10-26 23:00 的 103 不可用,故 101 于 10-27)
            req = server.state.use_requests[-1]
            assert req["resetType"] == "FIVE_HOUR" and req["recordId"] == 101
            assert req["targetType"] == "PERSONAL" and len(req["requestId"]) >= 32
            # 5h 额度归零
            assert server.state.limits[0]["currentValue"] == 0
            assert m.ui.has_log("已使用")
        finally:
            m.browser.close()

    def test_week_over_uses_week_card_and_resets_both(self, server, cfg):
        server.state.set_percent(60.0, 99.0)
        server.state.reset_cards()
        server.state.use_requests.clear()
        m = new_monitor(cfg())
        try:
            run_cycles(m)
            assert m.cards_used[strategy.WEEK] == 1
            req = server.state.use_requests[-1]
            assert req["resetType"] == "WEEK" and req["recordId"] == 202  # 10-27 08:00 最早
            assert server.state.limits[0]["currentValue"] == 0  # 5h 同步回满
            assert server.state.limits[1]["currentValue"] == 0
        finally:
            m.browser.close()

    def test_interval_switches_fast_and_slow(self, server, cfg):
        m = new_monitor(cfg(interval_slow=60, interval_fast=10, fast_5h=85, fast_week=95))
        try:
            server.state.set_percent(36.0, 83.0)   # 都低于快检阈值 -> 慢
            run_cycles(m)
            assert m._interval_now == 60
            server.state.set_percent(86.0, 83.0)   # 5h 过快检阈值 -> 快
            run_cycles(m)
            assert m._interval_now == 10
            server.state.set_percent(36.0, 96.0)   # 周过快检阈值 -> 快
            run_cycles(m)
            assert m._interval_now == 10
            server.state.set_percent(36.0, 50.0)   # 都回落 -> 慢
            run_cycles(m)
            assert m._interval_now == 60
        finally:
            m.browser.close()

    def test_independent_thresholds(self, server, cfg):
        # 5小时阈值 90:91% 即触发;周 96% 未达周阈值 97 -> 不动作
        server.state.set_percent(91.0, 96.0)
        server.state.reset_cards()
        server.state.use_requests.clear()
        m = new_monitor(cfg(threshold_5h=90, threshold_week=97))
        try:
            run_cycles(m)
            assert used(m) == 1
            assert server.state.use_requests[-1]["resetType"] == "FIVE_HOUR"
        finally:
            m.browser.close()


class TestBudgetAndSafety:
    def test_max_cards_budget_per_type(self, server, cfg):
        # 5小时卡预算 1(关闭兜底):用掉一张后,即使再次超阈值也不再动任何卡
        server.state.set_percent(99.0, 40.0)
        server.state.reset_cards()
        m = new_monitor(cfg(max_cards_5h=1, max_cards_week=5, fallback_week_for_5h=False))
        try:
            run_cycles(m)
            assert m.cards_used[strategy.FIVE_HOUR] == 1
            assert server.state.used_count() == 1
            server.state.set_percent(99.0, 40.0)  # 又用满了
            m.cooldown = {k: 0.0 for k in m.cooldown}  # 排除冷却因素,单测预算拦截
            run_cycles(m)
            assert m.cards_used[strategy.FIVE_HOUR] == 1
            assert server.state.used_count() == 1
            assert m.ui.has_log("5小时卡预算已用完")
        finally:
            m.browser.close()

    def test_5h_budget_exhausted_falls_back_week_card(self, server, cfg):
        # 5h 预算 0 且开启兜底 -> 用周卡,计入周卡预算
        server.state.set_percent(99.0, 40.0)
        server.state.reset_cards()
        server.state.use_requests.clear()
        m = new_monitor(cfg(max_cards_5h=0, max_cards_week=2, fallback_week_for_5h=True))
        try:
            run_cycles(m)
            assert m.cards_used[strategy.FIVE_HOUR] == 0
            assert m.cards_used[strategy.WEEK] == 1
            assert server.state.use_requests[-1]["resetType"] == "WEEK"
        finally:
            m.browser.close()

    def test_cooldown_blocks_immediate_second_use(self, server, cfg):
        server.state.set_percent(99.0, 99.0)
        server.state.reset_cards()
        m = new_monitor(cfg(max_cards_5h=3, max_cards_week=3, cooldown_sec=300))
        try:
            run_cycles(m)
            assert used(m) == 1  # 周卡一张把两个窗口都重置了
            # 人为再次把额度拉满,冷却期内不得再动卡(周卡冷却覆盖两个窗口)
            server.state.set_percent(99.0, 99.0)
            run_cycles(m)
            assert used(m) == 1 and server.state.used_count() == 1
            # 冷却结束后才允许第二张
            m.cooldown = {k: 0.0 for k in m.cooldown}
            run_cycles(m)
            assert used(m) == 2
        finally:
            m.browser.close()

    def test_5h_cooldown_does_not_block_week_trigger(self, server, cfg):
        # 5小时卡只冷却5小时窗口:冷却期间周额度到阈值应立即用周卡,不被拖延
        server.state.set_percent(99.0, 40.0)
        server.state.reset_cards()
        server.state.use_requests.clear()
        m = new_monitor(cfg(cooldown_sec=300))
        try:
            run_cycles(m)                       # 用 5h 卡
            assert m.cards_used[strategy.FIVE_HOUR] == 1
            assert time.time() < m.cooldown[strategy.FIVE_HOUR]   # 5h 窗口确在冷却
            server.state.set_percent(60.0, 99.9)  # 周额度到阈值
            run_cycles(m)                       # 不等 5h 冷却结束,立即用周卡
            assert m.cards_used[strategy.WEEK] == 1
            assert server.state.use_requests[-1]["resetType"] == "WEEK"
        finally:
            m.browser.close()

    def test_cooldown_display_clears_after_expiry(self, server, cfg):
        # 回归:冷却结束后界面不得残留"冷却 Ns"——每次检查都上报 cooldown,过期为 0
        server.state.set_percent(99.0, 40.0)
        server.state.reset_cards()
        m = new_monitor(cfg(cooldown_sec=300))
        try:
            run_cycles(m)                       # 用 5h 卡(状态上报发生在用卡之前)
            run_cycles(m)                       # 冷却期内的下一轮检查 -> cooldown>0
            assert m.ui.states[-1].get("cooldown", 0) > 0
            m.cooldown = {k: 0.0 for k in m.cooldown}  # 冷却到期
            run_cycles(m)
            assert m.ui.states[-1].get("cooldown") == 0  # 归 0,界面隐藏
        finally:
            m.browser.close()

    def test_dry_run_never_consumes(self, server, cfg):
        server.state.set_percent(99.0, 50.0)
        server.state.reset_cards()
        server.state.use_requests.clear()
        m = new_monitor(cfg(dry_run=True))
        try:
            run_cycles(m)
            assert server.state.used_count() == 0
            assert server.state.use_requests == []
            assert m.ui.has_log("[演练]")
        finally:
            m.browser.close()

    def test_auto_reset_off_never_consumes(self, server, cfg):
        server.state.set_percent(99.0, 99.0)
        server.state.reset_cards()
        m = new_monitor(cfg(auto_reset=False))
        try:
            run_cycles(m)
            assert server.state.used_count() == 0
        finally:
            m.browser.close()

    def test_business_failure_no_retry(self, server, cfg):
        # 业务失败(卡不可用):不消耗、进入冷却
        server.state.set_percent(99.0, 50.0)
        server.state.reset_cards()
        server.state.scenario["fail_use_once"] = True
        server.state.use_requests.clear()
        m = new_monitor(cfg())
        try:
            run_cycles(m)
            assert used(m) == 0
            assert server.state.used_count() == 0
            assert m.ui.has_log("使用失败")
            assert max(m.cooldown.values()) > time.time()
            # 冷却期内即使再检查也不重复调用
            n_calls = len(server.state.use_requests)
            run_cycles(m)
            assert len(server.state.use_requests) == n_calls
        finally:
            server.state.scenario.pop("fail_use_once", None)
            m.browser.close()

    def test_hang_no_blind_retry_no_consumption(self, server, cfg):
        # 超时(结果未知):核对状态后不重试、不消耗
        server.state.set_percent(99.0, 50.0)
        server.state.reset_cards()
        server.state.scenario["hang_use"] = True
        server.state.use_requests.clear()
        m = new_monitor(cfg())
        try:
            t0 = time.time()
            run_cycles(m)  # 页面内 fetch 15s 中止 -> 结果未知分支
            assert time.time() - t0 >= 14
            assert used(m) == 0
            assert server.state.used_count() == 0
            assert m.ui.has_log("结果未知") or m.ui.has_log("未生效")
            # 长冷却
            assert max(m.cooldown.values()) >= time.time() + 500
        finally:
            server.state.scenario.pop("hang_use", None)
            m.browser.close()


class TestLogin:
    def test_logged_out_detected(self, server, cfg):
        server.state.scenario["logged_out"] = True
        m = new_monitor(cfg())
        try:
            m._ensure_browser()
            assert m.browser.check_login() is False
            with pytest.raises(LoginRequired):
                m._cycle()
        finally:
            server.state.scenario.pop("logged_out", None)
            m.browser.close()

    def test_quota_api_shape(self, server, cfg):
        # 走完整浏览器链路验证额度解析(与真实接口同构的响应)
        server.state.set_percent(36.2, 83.49)
        m = new_monitor(cfg(auto_reset=False))
        try:
            run_cycles(m)
            snap = strategy.parse_quota(m.client.get_quota_raw())
            assert abs(snap.five_hour.percent - 36.2) < 0.05
            assert abs(snap.week.percent - 83.49) < 0.05
        finally:
            m.browser.close()
