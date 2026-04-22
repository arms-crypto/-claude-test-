"""
tests/unit/test_trading_logic.py
pytest tests/unit/test_trading_logic.py
"""
import pytest
from tests.unit.trading_logic import (
    calc_avg_price,
    calc_pnl_pct,
    calc_corrected_position,
    reconcile_holdings,
    fallback_sell_decision,
    calc_slot_allocation,
    validate_signal_threshold,
)


# ── 평균단가 계산 ─────────────────────────────────────────────────────────────

class TestCalcAvgPrice:
    def test_추가매수_평단_계산(self):
        # 10주 @1000 보유 중, 10주 @1200 추가매수
        result = calc_avg_price(10, 1000.0, 10, 1200.0)
        assert result == pytest.approx(1100.0)

    def test_신규매수_평단은_매수가(self):
        result = calc_avg_price(0, 0.0, 5, 50000.0)
        assert result == pytest.approx(50000.0)

    def test_동일가_추가매수_평단_불변(self):
        result = calc_avg_price(10, 1000.0, 5, 1000.0)
        assert result == pytest.approx(1000.0)

    def test_음수_수량_방어(self):
        result = calc_avg_price(-5, 1000.0, 0, 0.0)
        assert result == 0.0


# ── 손익률 계산 ──────────────────────────────────────────────────────────────

class TestCalcPnlPct:
    def test_익절(self):
        assert calc_pnl_pct(11000, 10000) == pytest.approx(10.0)

    def test_손절(self):
        assert calc_pnl_pct(9000, 10000) == pytest.approx(-10.0)

    def test_본전(self):
        assert calc_pnl_pct(10000, 10000) == pytest.approx(0.0)

    def test_평단가_0_방어(self):
        assert calc_pnl_pct(10000, 0) == 0.0


# ── on_fill 체결 보정 ─────────────────────────────────────────────────────────

class TestCalcCorrectedPosition:
    def test_완전체결_가격만_다름(self):
        # 낙관: 10주 @1000, 실체결: 10주 @1050
        # old_qty=10, old_avg=1000 (낙관 반영된 상태)
        qty, avg = calc_corrected_position(
            old_qty=10, old_avg=1000.0,
            expected_qty=10, expected_price=1000.0,
            fill_qty=10, fill_price=1050.0,
        )
        assert qty == 10
        assert avg == pytest.approx(1050.0)

    def test_부분체결_수량_적음(self):
        # 낙관: 10주 기록, 실체결: 7주만 체결
        # 기존 보유 20주 @900 있었고, 10주 추가 낙관기록 → 총 30주 @933
        old_qty, old_avg = 30, (20 * 900 + 10 * 1000) / 30
        qty, avg = calc_corrected_position(
            old_qty=old_qty, old_avg=old_avg,
            expected_qty=10, expected_price=1000.0,
            fill_qty=7, fill_price=1000.0,
        )
        assert qty == 27
        assert avg == pytest.approx((20 * 900 + 7 * 1000) / 27)

    def test_부분체결_3회_누적_평단(self):
        # 시나리오: 10주 낙관기록, 3회 분할체결 (3+3+4)
        # 1회 체결: 3주 @1000
        qty1, avg1 = calc_corrected_position(10, 1000.0, 10, 1000.0, 3, 1000.0)
        assert qty1 == 3
        # 2회 체결: 3주 @1010 (추가 매수처럼 평단 재계산)
        qty2 = qty1 + 3
        avg2 = calc_avg_price(qty1, avg1, 3, 1010.0)
        assert qty2 == 6
        assert avg2 == pytest.approx((3 * 1000 + 3 * 1010) / 6)
        # 3회 체결: 4주 @990
        qty3 = qty2 + 4
        avg3 = calc_avg_price(qty2, avg2, 4, 990.0)
        assert qty3 == 10
        assert avg3 == pytest.approx((3 * 1000 + 3 * 1010 + 4 * 990) / 10)

    def test_미체결_corrected_qty_0이하(self):
        # 낙관: 10주, 실체결: 0주 (전량 미체결)
        qty, avg = calc_corrected_position(10, 1000.0, 10, 1000.0, 0, 0.0)
        assert qty == 0

    def test_초과체결_방어(self):
        # 낙관: 10주, 실체결: 12주 (드문 케이스)
        qty, avg = calc_corrected_position(10, 1000.0, 10, 1000.0, 12, 1000.0)
        assert qty == 12


# ── sync_with_kis 보정 규칙 ───────────────────────────────────────────────────

class TestReconcileHoldings:
    def test_DB에만_있는_종목_삭제(self):
        db  = {"005930": {"qty": 10, "avg_price": 70000.0}}
        kis = {}
        delete, add, update = reconcile_holdings(db, kis)
        assert "005930" in delete
        assert add == []
        assert update == []

    def test_KIS에만_있는_종목_추가(self):
        db  = {}
        kis = {"000660": {"qty": 5, "avg_price": 130000.0, "name": "SK하이닉스"}}
        delete, add, update = reconcile_holdings(db, kis)
        assert delete == []
        assert len(add) == 1
        assert add[0]["code"] == "000660"
        assert add[0]["qty"] == 5

    def test_수량_불일치_KIS기준_업데이트(self):
        db  = {"005380": {"qty": 10, "avg_price": 200000.0}}
        kis = {"005380": {"qty": 8,  "avg_price": 200000.0, "name": "현대차"}}
        delete, add, update = reconcile_holdings(db, kis)
        assert delete == []
        assert add == []
        assert len(update) == 1
        assert update[0]["code"] == "005380"
        assert update[0]["qty"] == 8

    def test_완전일치_변경없음(self):
        db  = {"035720": {"qty": 3, "avg_price": 50000.0}}
        kis = {"035720": {"qty": 3, "avg_price": 50000.0, "name": "카카오"}}
        delete, add, update = reconcile_holdings(db, kis)
        assert delete == [] and add == [] and update == []

    def test_복합_시나리오(self):
        db  = {
            "005930": {"qty": 10, "avg_price": 70000.0},  # 삭제 대상
            "035720": {"qty": 3,  "avg_price": 50000.0},  # 수량 불일치
        }
        kis = {
            "000660": {"qty": 5, "avg_price": 130000.0, "name": "SK하이닉스"},  # 추가
            "035720": {"qty": 5, "avg_price": 50000.0,  "name": "카카오"},      # 수량 불일치
        }
        delete, add, update = reconcile_holdings(db, kis)
        assert "005930" in delete
        assert any(x["code"] == "000660" for x in add)
        assert any(x["code"] == "035720" and x["qty"] == 5 for x in update)


# ── 매도 폴백 규칙 ────────────────────────────────────────────────────────────

class TestFallbackSellDecision:
    # 단타
    def test_단타_익절(self):
        r = fallback_sell_decision(2.0, "단타")
        assert r["action"] == "SELL_ALL"

    def test_단타_익절_경계값(self):
        r = fallback_sell_decision(2.0, "단타")
        assert r["action"] == "SELL_ALL"
        r2 = fallback_sell_decision(1.99, "단타")
        assert r2["action"] == "HOLD"

    def test_단타_손절(self):
        r = fallback_sell_decision(-1.0, "단타")
        assert r["action"] == "SELL_ALL"

    def test_단타_손절_경계값(self):
        r = fallback_sell_decision(-1.0, "단타")
        assert r["action"] == "SELL_ALL"
        r2 = fallback_sell_decision(-0.99, "단타")
        assert r2["action"] == "HOLD"

    def test_단타_홀드_재확인_3분(self):
        r = fallback_sell_decision(0.5, "단타")
        assert r["action"] == "HOLD"
        assert r["check_after"] == 3

    # 스윙
    def test_스윙_익절_부분매도(self):
        r = fallback_sell_decision(5.0, "스윙")
        assert r["action"] == "SELL_PARTIAL"
        assert r["ratio"] == pytest.approx(0.3)

    def test_스윙_손절(self):
        r = fallback_sell_decision(-2.0, "스윙")
        assert r["action"] == "SELL_ALL"

    def test_스윙_손절_경계값(self):
        r = fallback_sell_decision(-2.0, "스윙")
        assert r["action"] == "SELL_ALL"
        r2 = fallback_sell_decision(-1.99, "스윙")
        assert r2["action"] == "HOLD"

    def test_스윙_홀드_재확인_15분(self):
        r = fallback_sell_decision(1.0, "스윙")
        assert r["action"] == "HOLD"
        assert r["check_after"] == 15

    def test_기본값은_스윙(self):
        r1 = fallback_sell_decision(1.0)
        r2 = fallback_sell_decision(1.0, "스윙")
        assert r1 == r2

    # 손절이 익절보다 우선 (경계값 테스트)
    def test_스윙_5퍼_정확히(self):
        r = fallback_sell_decision(5.0, "스윙")
        assert r["action"] == "SELL_PARTIAL"

    def test_스윙_4퍼_99_홀드(self):
        r = fallback_sell_decision(4.99, "스윙")
        assert r["action"] == "HOLD"


# ── 매수 금액 배분 ────────────────────────────────────────────────────────────

class TestCalcSlotAllocation:
    def test_기본_배분(self):
        # 잔고 7백만, 보유 0종목, max_slots=7 → 7백만/7 = 100만
        result = calc_slot_allocation(7_000_000, 0)
        assert result == 1_000_000

    def test_보유종목_있을때_잔여슬롯_줄어듦(self):
        # 잔고 6백만, 보유 3종목, 슬롯 4개 남음 → 6백만/4 = 150만
        result = calc_slot_allocation(6_000_000, 3)
        assert result == 1_500_000

    def test_보수적_운영_70퍼센트(self):
        result = calc_slot_allocation(7_000_000, 0, conservative=True)
        assert result == int(7_000_000 * 0.7 / 7)

    def test_최소금액_하한(self):
        result = calc_slot_allocation(100_000, 0)
        assert result == 50_000

    def test_최대금액_상한(self):
        result = calc_slot_allocation(100_000_000, 0, max_slots=1)
        assert result == 5_000_000

    def test_잔고_0일때_기본값_1백만(self):
        result = calc_slot_allocation(0, 0)
        assert result >= 50_000

    def test_보유가_슬롯수_초과해도_최소1슬롯(self):
        # 보유 10종목이지만 max_slots=7 → remain=1
        result = calc_slot_allocation(1_000_000, 10)
        assert result == 1_000_000


# ── 신호 임계값 검증 ─────────────────────────────────────────────────────────

class TestValidateSignalThreshold:
    def test_normal_6이상_통과(self):
        assert validate_signal_threshold(6, "normal") is True

    def test_normal_5이하_차단(self):
        assert validate_signal_threshold(5, "normal") is False

    def test_low_위험도_7이상_필요(self):
        assert validate_signal_threshold(7, "low") is True
        assert validate_signal_threshold(6, "low") is False

    def test_high_위험도_5이상_허용(self):
        assert validate_signal_threshold(5, "high") is True
        assert validate_signal_threshold(4, "high") is False

    def test_섹터_오버라이드_적용(self):
        overrides = {"반도체": 8}
        assert validate_signal_threshold(8, "normal", "반도체", overrides) is True
        assert validate_signal_threshold(7, "normal", "반도체", overrides) is False

    def test_섹터_오버라이드_없으면_기본값(self):
        assert validate_signal_threshold(6, "normal", "에너지", {}) is True
