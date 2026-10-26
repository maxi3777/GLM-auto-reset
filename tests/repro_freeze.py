"""复现脚本:用卡成功后 UI 是否卡死。GUI 主线程 pump + 监控线程,触发一次成功用卡后观察。"""
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mock_server import MockServer
from app.config import Config
from app.ui import AppUI

server = MockServer()
server.state.set_percent(99.0, 40.0)   # 5h 超阈值 -> 会用卡
server.state.reset_cards()

cfg = Config()
cfg.threshold_5h = 97
cfg.threshold_week = 97
cfg.interval_slow = 4
cfg.interval_fast = 2
cfg.usage_url = server.usage_url
cfg.profile_dir = str(tempfile.mkdtemp(prefix="repro_profile_"))

print("== 启动 UI(隐藏窗口)+ 监控线程 ==", flush=True)
ui = AppUI(cfg, start_monitor=False)
ui.root.withdraw()
ui.root.after(100, ui._poll)
ui.monitor.start()

t0 = time.time()
max_stall = 0.0
last_update = time.time()
pump_errors = []
observe_until = None
ok = False
try:
    while time.time() - t0 < 90:
        try:
            ui.root.update()
        except Exception as e:          # mainloop 中该异常会被吞掉;这里记录并继续泵
            pump_errors.append(repr(e))
        now = time.time()
        max_stall = max(max_stall, now - last_update)
        last_update = now

        used = sum(ui.monitor.cards_used.values())
        if used >= 1 and observe_until is None:
            observe_until = now + 15    # 用卡成功后再观察 15 秒(约 4 个周期)
            print(f"== 用卡成功(t+{now-t0:.1f}s),继续观察 15s ==", flush=True)
        if observe_until is not None and now >= observe_until:
            ok = True
            break
        time.sleep(0.02)

    print("== 结果 ==", flush=True)
    print(f"monitor alive: {ui.monitor.is_alive()}")
    print(f"used cards: {ui.monitor.cards_used}")
    print(f"max GUI stall: {max_stall:.2f}s")
    print(f"states cached: {len(ui.bridge._state_cache)}")
    print(f"notice in cache: {'notice' in ui.bridge._state_cache}")
    print(f"pump errors: {pump_errors[:3]}")
    assert ui.monitor.is_alive(), "监控线程死了"
    assert sum(ui.monitor.cards_used.values()) == 1, "恰好用了一张卡"
    assert max_stall < 2.0, f"GUI 主线程停顿 {max_stall:.2f}s —— 复现卡死!"
    assert "notice" in ui.bridge._state_cache, "通知未进入状态缓存(_notify bug)"
    print("PASS: 用卡成功后 UI 与监控均正常", flush=True)
finally:
    ui.monitor.stop()
    ui.monitor.join(timeout=8)
    try:
        ui.root.destroy()
    except Exception:
        pass
    server.stop()
