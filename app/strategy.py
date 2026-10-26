"""决策逻辑(纯函数,不依赖浏览器,便于单元测试)。

数据结构与官方接口一一对应:
- 额度  GET /api/monitor/usage/quota/limit
        data.limits[] = {type:"CREDIT_LIMIT", unit:3|6, number, usage, currentValue, remaining, percentage, nextResetTime}
        unit=3 -> 5小时额度;unit=6 -> 周额度(与官方前端 quotaUnit 映射一致)
- 重置卡 GET /api/biz/customer-package-reset/list?targetType=PERSONAL
        data.fiveHourResets[] / weekResets[] = {recordId, grantType, expireTime, available}
- 使用  POST /biz/customer-package-reset/use
        {targetType, resetType:"FIVE_HOUR"|"WEEK", recordId, grantType, requestId}
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

FIVE_HOUR = "FIVE_HOUR"
WEEK = "WEEK"

# 官方前端里 unit 与类型的映射: FIVE_HOUR -> 3, WEEK -> 6
UNIT_FOR_TYPE = {FIVE_HOUR: 3, WEEK: 6}


@dataclass
class Quota:
    """单个额度窗口的用量。percent 为精确已用百分比(currentValue/usage)。"""

    kind: str                      # FIVE_HOUR / WEEK
    used: float = 0.0              # currentValue 已用积分
    total: float = 0.0             # usage 总积分
    percent: float = 0.0           # 精确已用百分比
    next_reset_ts: Optional[int] = None  # nextResetTime(epoch ms)

    @property
    def valid(self) -> bool:
        return self.total > 0


@dataclass
class Card:
    record_id: int
    grant_type: str = ""
    expire_time: str = ""
    available: bool = True

    @property
    def priority(self) -> bool:
        """官方"优先"语义:该类型可用卡中有效期最早的那张。"""
        return False  # 由 CardPool.priority_card 计算


@dataclass
class CardPool:
    five_hour: list = field(default_factory=list)   # list[Card]
    week: list = field(default_factory=list)

    def available(self, kind: str) -> list:
        cards = self.five_hour if kind == FIVE_HOUR else self.week
        return [c for c in cards if c.available]

    def priority_card(self, kind: str) -> Optional[Card]:
        """可用卡中 expireTime 最早的一张(即官方 UI 标记"优先"的卡)。"""
        avail = self.available(kind)
        if not avail:
            return None
        return min(avail, key=lambda c: c.expire_time or "9999")


@dataclass
class Snapshot:
    five_hour: Optional[Quota] = None
    week: Optional[Quota] = None
    cards: Optional[CardPool] = None


@dataclass
class Decision:
    action: str            # "use_card" / "no_action" / "blocked"
    reason: str = ""
    reset_type: Optional[str] = None   # FIVE_HOUR / WEEK
    card: Optional[Card] = None
    is_fallback: bool = False          # 是否为"5小时卡用尽,用周卡兜底"


def parse_quota(payload: dict) -> Snapshot:
    """解析 /api/monitor/usage/quota/limit 的 JSON。"""
    snap = Snapshot()
    data = payload.get("data") or {}
    for lim in data.get("limits") or []:
        unit = lim.get("unit")
        kind = next((k for k, u in UNIT_FOR_TYPE.items() if u == unit), None)
        if kind is None:
            continue
        total = float(lim.get("usage") or 0)
        used = float(lim.get("currentValue") or 0)
        percent = (used / total * 100.0) if total > 0 else float(lim.get("percentage") or 0)
        q = Quota(kind=kind, used=used, total=total, percent=percent,
                  next_reset_ts=lim.get("nextResetTime"))
        if kind == FIVE_HOUR:
            snap.five_hour = q
        else:
            snap.week = q
    return snap


def parse_cards(payload: dict) -> CardPool:
    """解析 /api/biz/customer-package-reset/list 的 JSON。"""
    data = payload.get("data") or {}
    pool = CardPool()
    for item in data.get("fiveHourResets") or []:
        pool.five_hour.append(Card(
            record_id=item.get("recordId") or 0,
            grant_type=item.get("grantType") or "",
            expire_time=item.get("expireTime") or "",
            available=bool(item.get("available")),
        ))
    for item in data.get("weekResets") or []:
        pool.week.append(Card(
            record_id=item.get("recordId") or 0,
            grant_type=item.get("grantType") or "",
            expire_time=item.get("expireTime") or "",
            available=bool(item.get("available")),
        ))
    return pool


def needs_reset(quota: Optional[Quota], threshold: float) -> bool:
    return quota is not None and quota.valid and quota.percent >= threshold


def fast_check_mode(snapshot: Snapshot, fast_5h: float, fast_week: float) -> bool:
    """任一额度达到快检查阈值(>=)即进入快检查。"""
    return (needs_reset(snapshot.five_hour, fast_5h)
            or needs_reset(snapshot.week, fast_week))


def decide(snapshot: Snapshot, threshold_5h: float, threshold_week: float,
           remaining: dict, fallback_week_for_5h: bool,
           cooling: Optional[dict] = None) -> Decision:
    """根据额度与卡池决定动作。

    参数 remaining: {FIVE_HOUR: 剩余可用张数, WEEK: 剩余可用张数}(本轮预算)
    参数 cooling: {FIVE_HOUR: bool, WEEK: bool} 对应额度窗口是否处于用卡后的冷却期

    规则:
    1. 周额度 >= 周阈值 -> 用周卡(周卡会同步回满5h,一张解决两个窗口)。
    2. 仅5小时 >= 5小时阈值 -> 用5小时卡;若无5小时卡/预算且允许兜底 -> 用周卡。
    3. 选中类型内取"优先"卡(expireTime 最早的可用卡)。
    4. 触发窗口处于冷却期(刚用过卡,等用量数据刷新) -> 暂不动作,冷却只挡对应窗口:
       5小时卡只冷却5小时窗口;周卡重置两个窗口,冷却两个窗口。
    """
    cooling = cooling or {}
    w_over = needs_reset(snapshot.week, threshold_week)
    f_over = needs_reset(snapshot.five_hour, threshold_5h)

    if not w_over and not f_over:
        return Decision("no_action", "额度未达阈值")

    if w_over and cooling.get(WEEK):
        return Decision("cooldown", "周额度已达阈值,周窗口冷却中,稍后自动重试")
    if f_over and cooling.get(FIVE_HOUR):
        return Decision("cooldown", "5小时额度已达阈值,5小时窗口冷却中,稍后自动重试")

    if snapshot.cards is None:
        return Decision("blocked", "尚未获取重置卡列表")

    if w_over:
        if remaining.get(WEEK, 0) <= 0:
            return Decision("blocked", "周额度已达阈值,但本轮周卡预算已用完")
        card = snapshot.cards.priority_card(WEEK)
        if card is None:
            return Decision("blocked", "周额度已超阈值,但没有可用的周重置卡")
        return Decision("use_card", "周额度已达阈值,使用周卡(同步回满5小时额度)",
                        reset_type=WEEK, card=card)

    # 仅 5 小时超阈值
    if remaining.get(FIVE_HOUR, 0) > 0:
        card = snapshot.cards.priority_card(FIVE_HOUR)
        if card is not None:
            return Decision("use_card", "5小时额度已达阈值,使用5小时卡",
                            reset_type=FIVE_HOUR, card=card)
    if fallback_week_for_5h and remaining.get(WEEK, 0) > 0:
        wcard = snapshot.cards.priority_card(WEEK)
        if wcard is not None:
            reason = ("5小时卡预算已用完" if remaining.get(FIVE_HOUR, 0) <= 0
                      else "无可用5小时卡")
            return Decision("use_card", f"{reason},使用周卡兜底",
                            reset_type=WEEK, card=wcard, is_fallback=True)
    if remaining.get(FIVE_HOUR, 0) <= 0:
        return Decision("blocked", "5小时额度已超阈值,但本轮5小时卡预算已用完")
    return Decision("blocked", "5小时额度已超阈值,但没有可用的5小时重置卡")
