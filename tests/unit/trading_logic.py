"""
trading_logic.py — 순수 비즈니스 로직 (외부 의존성 없음)

mock_trading.py / auto_trader.py 에서 추출한 순수함수들.
단위 테스트 대상이며, DB/KIS/LLM 호출 없음.
"""


# ── 포지션/평단 계산 ─────────────────────────────────────────────────────────

def calc_avg_price(old_qty: int, old_avg: float, add_qty: int, add_price: float) -> float:
    """추가 매수 후 평균단가 계산."""
    new_qty = old_qty + add_qty
    if new_qty <= 0:
        return 0.0
    return (old_qty * old_avg + add_qty * add_price) / new_qty


def calc_pnl_pct(current_price: float, avg_price: float) -> float:
    """손익률(%) 계산."""
    if avg_price <= 0:
        return 0.0
    return (current_price - avg_price) / avg_price * 100


# ── on_fill 체결 보정 ────────────────────────────────────────────────────────

def calc_corrected_position(
    old_qty: int, old_avg: float,
    expected_qty: int, expected_price: float,
    fill_qty: int, fill_price: float,
) -> tuple[int, float]:
    """
    낙관적 DB 기록을 실체결 기준으로 보정.

    Returns:
        (corrected_qty, corrected_avg) — corrected_qty <= 0 이면 포지션 삭제
    """
    corrected_qty = old_qty - expected_qty + fill_qty
    if corrected_qty <= 0:
        return (0, 0.0)
    corrected_cost = (old_qty * old_avg
                      - expected_qty * expected_price
                      + fill_qty * fill_price)
    return (corrected_qty, corrected_cost / corrected_qty)


# ── sync_with_kis 보정 규칙 ──────────────────────────────────────────────────

def reconcile_holdings(
    db_holdings: dict,   # {code: {"qty": int, "avg_price": float}}
    kis_holdings: dict,  # {code: {"qty": int, "avg_price": float, "name": str}}
) -> tuple[list, list, list]:
    """
    DB와 KIS 잔고 불일치 분석. KIS를 신뢰 소스로 사용.

    Returns:
        (to_delete, to_add, to_update)
        - to_delete: [code, ...]
        - to_add:    [{"code", "name", "qty", "avg_price"}, ...]
        - to_update: [{"code", "qty", "avg_price"}, ...]
    """
    to_delete = [code for code in db_holdings if code not in kis_holdings]
    to_add    = [
        {"code": code, **h}
        for code, h in kis_holdings.items()
        if code not in db_holdings
    ]
    to_update = [
        {"code": code, "qty": h["qty"], "avg_price": h["avg_price"]}
        for code, h in kis_holdings.items()
        if code in db_holdings and db_holdings[code]["qty"] != h["qty"]
    ]
    return (to_delete, to_add, to_update)


# ── 매도 폴백 규칙 ───────────────────────────────────────────────────────────

def fallback_sell_decision(pnl: float, trade_type: str = "스윙") -> dict:
    """
    LLM 실패 시 폴백 매도 규칙.
    specs/trading-rules.md §1 기준.
    """
    if trade_type == "단타":
        if pnl >= 2:
            return {"action": "SELL_ALL",  "ratio": 1.0, "check_after": 0,  "reason": "폴백 단타 +2% 익절"}
        if pnl <= -1:
            return {"action": "SELL_ALL",  "ratio": 1.0, "check_after": 0,  "reason": "폴백 단타 -1% 손절"}
        return     {"action": "HOLD",      "ratio": 0.0, "check_after": 3,  "reason": "폴백 단타 유지"}
    else:
        if pnl >= 5:
            return {"action": "SELL_PARTIAL", "ratio": 0.3, "check_after": 10, "reason": "폴백 스윙 +5% 익절"}
        if pnl <= -2:
            return {"action": "SELL_ALL",     "ratio": 1.0, "check_after": 0,  "reason": "폴백 스윙 -2% 손절"}
        return     {"action": "HOLD",         "ratio": 0.0, "check_after": 15, "reason": "폴백 스윙 유지"}


# ── 매수 금액 배분 ───────────────────────────────────────────────────────────

def calc_slot_allocation(
    cash: float,
    num_holdings: int,
    conservative: bool = False,
    max_slots: int = 7,
    min_amount: int = 50_000,
    max_amount: int = 5_000_000,
) -> int:
    """
    잔여 슬롯 기준 매수 배정금액 계산.
    specs/trading-rules.md §5 기준.
    """
    if cash <= 0:
        cash = 1_000_000
    remain = max(1, max_slots - num_holdings)
    effective = cash * 0.7 if conservative else cash
    amount = int(effective / remain)
    return max(min_amount, min(max_amount, amount))


# ── 신호 임계값 검증 ─────────────────────────────────────────────────────────

_RISK_THRESHOLDS = {"low": 7, "normal": 6, "high": 5}


def validate_signal_threshold(
    buy_count: int,
    risk_level: str = "normal",
    sector: str = "",
    overrides: dict = None,
) -> bool:
    """
    PC 전략 기준 매수 신호 임계값 통과 여부.
    specs/trading-rules.md §6 기준.
    """
    overrides = overrides or {}
    threshold = overrides.get(sector, _RISK_THRESHOLDS.get(risk_level, 6))
    return buy_count >= threshold
