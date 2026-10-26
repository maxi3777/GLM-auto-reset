"""纯逻辑单元测试:额度解析、卡选择、决策分支。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import strategy


def quota_payload(p5=36.2, pw=83.49):
    return {
        "code": 200, "success": True,
        "data": {"limits": [
            {"type": "CREDIT_LIMIT", "unit": 3, "number": 5, "usage": 2000,
             "currentValue": round(2000 * p5 / 100), "remaining": 1, "percentage": int(p5),
             "nextResetTime": 1},
            {"type": "CREDIT_LIMIT", "unit": 6, "number": 1, "usage": 10000,
             "currentValue": round(10000 * pw / 100), "remaining": 1, "percentage": int(pw),
             "nextResetTime": 2},
        ], "level": "lite"},
    }


def cards_payload(five_hour=3, week=2, week_extra=False):
    fh = [{"recordId": 100 + i, "grantType": "DIRECT",
           "expireTime": f"2026-10-{28 - i} 14:00:00", "available": True}
          for i in range(five_hour)]
    fh.append({"recordId": 199, "grantType": "DIRECT",
               "expireTime": "2026-09-01 00:00:00", "available": False})
    wk = [{"recordId": 200 + i, "grantType": "DIRECT",
           "expireTime": f"2026-10-{27 - i} 10:00:00", "available": True}
          for i in range(week)]
    if week_extra:
        wk.append({"recordId": 999, "grantType": "GLOBAL",
                   "expireTime": "2026-12-22 23:59:59", "available": True})
    return {"code": 200, "success": True,
            "data": {"fiveHourResets": fh, "weekResets": wk}}


class TestParseQuota:
    def test_units(self):
        snap = strategy.parse_quota(quota_payload())
        assert snap.five_hour.kind == strategy.FIVE_HOUR
        assert snap.week.kind == strategy.WEEK
        assert abs(snap.five_hour.percent - 36.2) < 0.01
        assert abs(snap.week.percent - 83.49) < 0.01

    def test_precision_not_rounded(self):
        # 96.95 必须按精确值参与阈值判断,不能用整数字段
        snap = strategy.parse_quota(quota_payload(p5=96.95, pw=10))
        assert abs(snap.five_hour.percent - 96.95) < 0.01
        assert snap.five_hour.percent < 97

    def test_empty(self):
        snap = strategy.parse_quota({"data": {"limits": []}})
        assert snap.five_hour is None and snap.week is None


class TestParseCards:
    def test_parse(self):
        pool = strategy.parse_cards(cards_payload())
        assert len(pool.available(strategy.FIVE_HOUR)) == 3
        assert len(pool.available(strategy.WEEK)) == 2

    def test_priority_is_earliest_expiry(self):
        pool = strategy.parse_cards(cards_payload())
        # 5h 可用卡有效期 10-26/10-27/10-28 -> 最早是 10-26(recordId 102)
        pri = pool.priority_card(strategy.FIVE_HOUR)
        assert pri.record_id == 102 and pri.expire_time.startswith("2026-10-26")
        wk = pool.priority_card(strategy.WEEK)
        assert wk.record_id == 201  # 10-26 比 10-27 早


class TestFastCheckMode:
    def test_below_both_slow(self):
        snap = strategy.parse_quota(quota_payload(84.9, 94.9))
        assert strategy.fast_check_mode(snap, 85, 95) is False

    def test_5h_reaches_fast(self):
        snap = strategy.parse_quota(quota_payload(85.0, 10))
        assert strategy.fast_check_mode(snap, 85, 95) is True

    def test_week_reaches_fast(self):
        snap = strategy.parse_quota(quota_payload(10, 95.0))
        assert strategy.fast_check_mode(snap, 85, 95) is True

    def test_fast_thresholds_independent(self):
        snap = strategy.parse_quota(quota_payload(80, 90))
        assert strategy.fast_check_mode(snap, 85, 95) is False
        assert strategy.fast_check_mode(snap, 75, 95) is True


FULL_BUDGET = {strategy.FIVE_HOUR: 9, strategy.WEEK: 9}


class TestCooling:
    def test_week_cooling_delays_week_action(self):
        snap = strategy.parse_quota(quota_payload(50, 99))
        snap.cards = strategy.parse_cards(cards_payload())
        d = strategy.decide(snap, 97, 97, FULL_BUDGET, True,
                            cooling={strategy.WEEK: True})
        assert d.action == "cooldown"

    def test_5h_cooling_delays_5h_action(self):
        snap = strategy.parse_quota(quota_payload(99, 50))
        snap.cards = strategy.parse_cards(cards_payload())
        d = strategy.decide(snap, 97, 97, FULL_BUDGET, True,
                            cooling={strategy.FIVE_HOUR: True})
        assert d.action == "cooldown"

    def test_5h_cooling_does_not_block_week(self):
        # 5小时卡只冷却5小时窗口:周额度触发不受影响
        snap = strategy.parse_quota(quota_payload(50, 99))
        snap.cards = strategy.parse_cards(cards_payload())
        d = strategy.decide(snap, 97, 97, FULL_BUDGET, True,
                            cooling={strategy.FIVE_HOUR: True})
        assert d.action == "use_card" and d.reset_type == strategy.WEEK

    def test_week_cooling_covers_both_windows(self):
        # 周卡重置两个窗口:5h 冷却 + 5h 超阈值 -> 也不得再动 5h 卡
        snap = strategy.parse_quota(quota_payload(99, 50))
        snap.cards = strategy.parse_cards(cards_payload())
        d = strategy.decide(snap, 97, 97, FULL_BUDGET, True,
                            cooling={strategy.FIVE_HOUR: True, strategy.WEEK: True})
        assert d.action == "cooldown"

    def test_no_cooling_unchanged(self):
        snap = strategy.parse_quota(quota_payload(99, 50))
        snap.cards = strategy.parse_cards(cards_payload())
        d = strategy.decide(snap, 97, 97, FULL_BUDGET, True, cooling={})
        assert d.action == "use_card"


class TestDecide:
    def test_below_threshold_no_action(self):
        snap = strategy.parse_quota(quota_payload(36, 83))
        snap.cards = strategy.parse_cards(cards_payload())
        d = strategy.decide(snap, 97, 97, FULL_BUDGET, True)
        assert d.action == "no_action"

    def test_only_5h_over_uses_five_hour_card(self):
        snap = strategy.parse_quota(quota_payload(97.5, 50))
        snap.cards = strategy.parse_cards(cards_payload())
        d = strategy.decide(snap, 97, 97, FULL_BUDGET, True)
        assert d.action == "use_card" and d.reset_type == strategy.FIVE_HOUR
        assert d.card.record_id == 102  # 优先卡

    def test_only_week_over_uses_week_card(self):
        snap = strategy.parse_quota(quota_payload(50, 98))
        snap.cards = strategy.parse_cards(cards_payload())
        d = strategy.decide(snap, 97, 97, FULL_BUDGET, True)
        assert d.action == "use_card" and d.reset_type == strategy.WEEK
        assert d.card.record_id == 201

    def test_both_over_prefers_week(self):
        # 周卡一张解决两个窗口
        snap = strategy.parse_quota(quota_payload(98, 99))
        snap.cards = strategy.parse_cards(cards_payload())
        d = strategy.decide(snap, 97, 97, FULL_BUDGET, True)
        assert d.reset_type == strategy.WEEK

    def test_independent_thresholds(self):
        # 5小时阈值 90、周阈值 97:5h=91 触发 5h 卡;周=96 不触发周卡
        snap = strategy.parse_quota(quota_payload(91, 96))
        snap.cards = strategy.parse_cards(cards_payload())
        d = strategy.decide(snap, 90, 97, FULL_BUDGET, True)
        assert d.action == "use_card" and d.reset_type == strategy.FIVE_HOUR
        # 反过来:5h=89(未达90) 周=97.5(达到97) -> 周卡
        snap2 = strategy.parse_quota(quota_payload(89, 97.5))
        snap2.cards = strategy.parse_cards(cards_payload())
        d2 = strategy.decide(snap2, 90, 97, FULL_BUDGET, True)
        assert d2.action == "use_card" and d2.reset_type == strategy.WEEK

    def test_week_budget_exhausted_blocks(self):
        snap = strategy.parse_quota(quota_payload(50, 99))
        snap.cards = strategy.parse_cards(cards_payload())
        d = strategy.decide(snap, 97, 97, {strategy.FIVE_HOUR: 9, strategy.WEEK: 0}, True)
        assert d.action == "blocked" and "周卡预算" in d.reason

    def test_5h_budget_exhausted_falls_back_to_week(self):
        snap = strategy.parse_quota(quota_payload(99, 50))
        snap.cards = strategy.parse_cards(cards_payload())
        d = strategy.decide(snap, 97, 97, {strategy.FIVE_HOUR: 0, strategy.WEEK: 2}, True)
        assert d.action == "use_card" and d.reset_type == strategy.WEEK and d.is_fallback

    def test_5h_budget_exhausted_no_fallback(self):
        snap = strategy.parse_quota(quota_payload(99, 50))
        snap.cards = strategy.parse_cards(cards_payload())
        d = strategy.decide(snap, 97, 97, {strategy.FIVE_HOUR: 0, strategy.WEEK: 2}, False)
        assert d.action == "blocked"

    def test_5h_over_no_5h_cards_fallback_week(self):
        snap = strategy.parse_quota(quota_payload(99, 50))
        snap.cards = strategy.parse_cards(cards_payload(five_hour=0, week=2))
        d = strategy.decide(snap, 97, 97, FULL_BUDGET, True)
        assert d.action == "use_card" and d.reset_type == strategy.WEEK and d.is_fallback

    def test_5h_over_no_5h_cards_no_fallback(self):
        snap = strategy.parse_quota(quota_payload(99, 50))
        snap.cards = strategy.parse_cards(cards_payload(five_hour=0, week=2))
        d = strategy.decide(snap, 97, 97, FULL_BUDGET, False)
        assert d.action == "blocked"

    def test_week_over_no_week_cards(self):
        snap = strategy.parse_quota(quota_payload(50, 99))
        snap.cards = strategy.parse_cards(cards_payload(3, 0))
        d = strategy.decide(snap, 97, 97, FULL_BUDGET, True)
        assert d.action == "blocked"

    def test_threshold_boundary_97_exactly(self):
        snap = strategy.parse_quota(quota_payload(97.0, 50))
        snap.cards = strategy.parse_cards(cards_payload())
        d = strategy.decide(snap, 97, 97, FULL_BUDGET, True)
        assert d.action == "use_card"

    def test_just_below_97(self):
        snap = strategy.parse_quota(quota_payload(96.9, 50))
        snap.cards = strategy.parse_cards(cards_payload())
        d = strategy.decide(snap, 97, 97, FULL_BUDGET, True)
        assert d.action == "no_action"
