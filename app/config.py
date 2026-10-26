"""配置加载与保存。"""
from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"


@dataclass
class Config:
    # ---- 触发阈值:已用百分比 >= 阈值时触发重置(精确值,按 currentValue/usage 计算)----
    threshold_5h: float = 97.0
    threshold_week: float = 97.0
    # ---- 每种卡在程序一次运行期间的最大使用数(硬上限,重启才重置)----
    max_cards_5h: int = 2
    max_cards_week: int = 2
    # ---- 双速轮询:慢检查/快检查间隔(秒) ----
    interval_slow: int = 60
    interval_fast: int = 10
    # ---- 快检查开启阈值:5小时或周额度任一达到对应阈值即进入快检查 ----
    fast_5h: float = 85.0
    fast_week: float = 95.0
    # ---- 其他 ----
    auto_reset: bool = True
    dry_run: bool = False
    headless: bool = True
    fallback_week_for_5h: bool = True
    cooldown_sec: int = 300
    usage_url: str = "https://bigmodel.cn/coding-plan/personal/usage"
    profile_dir: str = "browser_profile"

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def save(self) -> None:
        data = {f.name: getattr(self, f.name) for f in fields(self) if not f.name.startswith("_")}
        CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        if CONFIG_PATH.exists():
            try:
                data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                # 旧版单阈值/单预算字段迁移
                if "threshold" in data:
                    data.setdefault("threshold_5h", data["threshold"])
                    data.setdefault("threshold_week", data["threshold"])
                if "max_cards" in data:
                    data.setdefault("max_cards_5h", data["max_cards"])
                    data.setdefault("max_cards_week", data["max_cards"])
                if "interval_sec" in data:
                    data.setdefault("interval_slow", data["interval_sec"])
                for f in fields(cls):
                    if f.name.startswith("_"):
                        continue
                    if f.name in data:
                        setattr(cfg, f.name, data[f.name])
            except (json.JSONDecodeError, OSError):
                pass
        cfg.validate()
        return cfg

    def validate(self) -> None:
        self.threshold_5h = min(100.0, max(50.0, float(self.threshold_5h)))
        self.threshold_week = min(100.0, max(50.0, float(self.threshold_week)))
        self.fast_5h = min(100.0, max(0.0, float(self.fast_5h)))
        self.fast_week = min(100.0, max(0.0, float(self.fast_week)))
        self.max_cards_5h = min(9, max(0, int(self.max_cards_5h)))
        self.max_cards_week = min(9, max(0, int(self.max_cards_week)))
        self.interval_slow = min(3600, max(10, int(self.interval_slow)))
        self.interval_fast = min(self.interval_slow, max(5, int(self.interval_fast)))
        self.cooldown_sec = min(3600, max(30, int(self.cooldown_sec)))

    def snapshot(self) -> dict:
        with self._lock:
            return asdict(self)
