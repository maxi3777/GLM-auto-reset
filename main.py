"""GLM 用量自动重置 —— 入口。

用法:
    python main.py          # 启动 UI 与监控
    python main.py --once   # 无 UI,检查一次并打印结果(不自动用卡,可配 dry-run)
"""
from __future__ import annotations

import sys


def run_gui() -> None:
    from app.config import Config
    from app.ui import AppUI
    AppUI(Config.load())


def run_once() -> int:
    """一次性检查:只读接口,打印额度与重置卡状态,不做任何写操作。"""
    from app.config import Config
    from app.monitor import Monitor
    from app import strategy

    cfg = Config.load()
    cfg.auto_reset = False  # 强制只读

    class PrintUI:
        def post_log(self, msg, level="info"):
            print(f"[{level}] {msg}")

        def post_state(self, **kw):
            pass

        def post_notify(self, msg):
            print(f"[通知] {msg}")

    m = Monitor(cfg, PrintUI())
    m._ensure_browser()

    def logged_in() -> bool:
        url = m.browser.current_url()
        if "bigmodel.cn" in url:
            return m.browser.check_login() and m.browser.has_token()
        return m.browser.check_login()

    try:
        if not logged_in():
            print("需要登录:请在弹出的浏览器窗口中完成登录(最长等待 3 分钟)…")
            if m.browser.headless:
                m.browser.escalate_headful("需要登录")
                m.browser.goto_usage()
            import time
            deadline = time.time() + 180
            while time.time() < deadline and not logged_in():
                time.sleep(5)
            if not logged_in():
                print("等待登录超时,退出。登录成功后再次运行即可。")
                return 1
        quota = strategy.parse_quota(m.client.get_quota_raw())
        cards = strategy.parse_cards(m.client.get_cards_raw())
        for q in (quota.five_hour, quota.week):
            if q:
                name = "5小时额度" if q.kind == strategy.FIVE_HOUR else "周额度"
                print(f"{name}: {q.percent:.1f}% 已用 ({q.used:.0f}/{q.total:.0f})")
        print(f"可用重置卡: 5小时 {len(cards.available(strategy.FIVE_HOUR))} 张, "
              f"周 {len(cards.available(strategy.WEEK))} 张")
        for kind, pool in (("5小时", cards.five_hour), ("周", cards.week)):
            for c in pool:
                mark = "可用" if c.available else "不可用"
                pri = " [优先]" if (c.available and cards.priority_card(
                    strategy.FIVE_HOUR if kind == "5小时" else strategy.WEEK).record_id == c.record_id) else ""
                print(f"  {kind}卡 recordId={c.record_id} grantType={c.grant_type} "
                      f"有效期至 {c.expire_time} {mark}{pri}")
        return 0
    finally:
        m.browser.close()


if __name__ == "__main__":
    if "--once" in sys.argv:
        sys.exit(run_once())
    run_gui()
