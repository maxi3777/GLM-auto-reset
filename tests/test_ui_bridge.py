"""UI 桥与 UI 泵的回归测试。

背景 bug:用卡成功后 post_notify 触发 `_apply_state(notice=...)` TypeError,
异常发生在 UI 主线程的 _poll 回调里导致 after 链断裂,界面永久停更/卡死。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ui import AppUI, UIBridge
from app.config import Config


class TestUIBridge:
    def test_notify_and_events_drain(self):
        b = UIBridge()  # 无控件也应可用
        b.post_log("hello", "ok")
        b.post_state(five_hour_pct="36%")
        b.post_notify("已使用5小时重置卡")
        b.drain()
        assert b._state_cache.get("five_hour_pct") == "36%"
        assert b._state_cache.get("notice") == "已使用5小时重置卡"  # 回归:此前在此抛 TypeError

    def test_drain_survives_bad_event(self):
        b = UIBridge()
        b.q.put(("state", None))       # 恶意/异常事件
        b.post_state(week_pct="80%")
        b.drain()                      # 不得抛出
        assert b._state_cache.get("week_pct") == "80%"  # 后续事件仍被处理


@pytest.fixture
def ui():
    try:
        app = AppUI(Config.load(), start_monitor=False)
    except Exception as e:  # 无显示环境时跳过
        pytest.skip(f"Tk 不可用:{e}")
    app.root.withdraw()
    yield app
    try:
        app.root.destroy()
    except Exception:
        pass


class TestPollResilience:
    def test_poll_re_registers_after_render_exception(self, ui):
        # _render 抛异常时,_poll 不得中断 after 链,也不得向外抛
        original = ui._render
        boom = RuntimeError("渲染爆炸")
        ui._render = lambda st: (_ for _ in ()).throw(boom)
        try:
            ui._poll()   # 不得抛出
            ui._poll()   # 再次调用仍安全(证明可被反复调度)
        finally:
            ui._render = original
        # 修复后的 _render 正常工作
        ui.bridge.post_state(five_hour_pct="42%", week_pct="50%")
        ui.bridge.drain()
        ui._render(ui.bridge._state_cache)
        assert ui.bar_5h[1].get() == "42%"

    def test_poll_renders_and_reschedules(self, ui):
        ui.bridge.post_state(five_hour_pct="10%", week_pct="20%",
                             cards_used_5h=0, cards_used_week=1,
                             max_cards_5h=2, max_cards_week=3)
        ui._poll()
        assert ui.bar_week[1].get() == "20%"
        assert "周 1/3" in ui.var_used.get()
        # after 链仍在:存在待触发的 _poll 回调(after info 返回 'after#N' 列表)
        pending = ui.root.tk.call("after", "info")
        assert pending and any(str(p).startswith("after#") for p in pending), pending
