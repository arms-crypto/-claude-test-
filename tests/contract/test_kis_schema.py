"""
2주차 계약 테스트 — KIS API 응답 스키마 검증
실제 API를 호출하지 않고, kis_client.py가 반환하는 dict 구조가
시스템 전체에서 기대하는 스키마를 따르는지 검증한다.

핵심 원칙:
- 실제 KIS API 호출 없음 (mock 사용)
- 파싱 로직(dict 변환)만 검증
- 스키마 계약이 깨지면 자동매매 전체가 멈춤 → 조기 탐지
"""
import pytest
from unittest.mock import patch, MagicMock


# ── 스키마 상수 (전체 시스템에서 기대하는 구조) ──────────────────────────

BALANCE_SCHEMA = {
    "cash": int,
    "holdings": list,
}

HOLDING_ITEM_SCHEMA = {
    "code":          str,
    "name":          str,
    "qty":           int,
    "sell_qty":      int,
    "avg_price":     float,
    "current_price": int,
    "pnl":           float,
}

BUY_SELL_SCHEMA = {
    "success":  bool,
    "order_no": str,
    "msg":      str,
}


# ── 헬퍼 ──────────────────────────────────────────────────────────────────

def _fake_kis_balance_response():
    """KIS TTTC8434R 잔고조회 실제 응답 형식 모사."""
    return {
        "rt_cd": "0",
        "msg1": "정상처리 되었습니다.",
        "output1": [
            {
                "pdno":         "005930",
                "prdt_name":    "삼성전자",
                "hldg_qty":     "10",
                "ord_psbl_qty": "10",
                "pchs_avg_pric": "70000.0",
                "prpr":         "72000",
                "evlu_pfls_rt": "2.86",
            }
        ],
        "output2": [
            {"dnca_tot_amt": "5000000"}
        ],
    }


def _fake_kis_order_response(success: bool = True):
    """KIS 주문 성공/실패 응답 형식 모사."""
    if success:
        return {"rt_cd": "0", "msg1": "정상처리", "output": {"ODNO": "0001234567"}}
    return {"rt_cd": "1", "msg1": "잔고부족"}


def _fake_kis_price_response(price: int = 72000):
    """KIS 주가 조회 응답 형식 모사."""
    return {
        "rt_cd": "0",
        "output": {"stck_prpr": str(price)},
    }


# ── get_balance() 스키마 계약 테스트 ──────────────────────────────────────

class TestGetBalanceSchema:

    def _call_get_balance(self, api_response):
        """mock requests로 get_balance() 호출."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = api_response
        mock_resp.raise_for_status.return_value = None

        with patch("mock_trading.kis_client.requests.get", return_value=mock_resp), \
             patch("mock_trading.kis_client.get_token", return_value="dummy_token"), \
             patch("auto_trader.is_nxt_hours", return_value=False):
            from mock_trading.kis_client import get_balance
            return get_balance()

    def test_반환타입_dict(self):
        result = self._call_get_balance(_fake_kis_balance_response())
        assert isinstance(result, dict), "get_balance()는 dict를 반환해야 한다"

    def test_cash_키_존재(self):
        result = self._call_get_balance(_fake_kis_balance_response())
        assert "cash" in result, "결과에 'cash' 키가 있어야 한다"

    def test_holdings_키_존재(self):
        result = self._call_get_balance(_fake_kis_balance_response())
        assert "holdings" in result, "결과에 'holdings' 키가 있어야 한다"

    def test_cash_타입_int(self):
        result = self._call_get_balance(_fake_kis_balance_response())
        assert isinstance(result["cash"], int), f"cash는 int여야 한다 (got {type(result['cash'])})"

    def test_holdings_타입_list(self):
        result = self._call_get_balance(_fake_kis_balance_response())
        assert isinstance(result["holdings"], list), "holdings는 list여야 한다"

    def test_holding_항목_스키마(self):
        result = self._call_get_balance(_fake_kis_balance_response())
        assert len(result["holdings"]) > 0, "보유종목이 1개 이상이어야 한다"
        item = result["holdings"][0]
        for key, expected_type in HOLDING_ITEM_SCHEMA.items():
            assert key in item, f"holding 항목에 '{key}' 키가 없다"
            assert isinstance(item[key], expected_type), (
                f"holding['{key}']는 {expected_type.__name__}여야 한다 "
                f"(got {type(item[key]).__name__}: {item[key]!r})"
            )

    def test_네트워크_실패시_폴백(self):
        """토큰 없을 때 빈 결과 반환 (예외 아님)."""
        with patch("mock_trading.kis_client.get_token", return_value=None):
            from mock_trading.kis_client import get_balance
            result = get_balance()
        assert result == {"cash": 0, "holdings": []}, "토큰 없을 때 기본값 반환해야 한다"

    def test_qty_0인_항목_제외(self):
        """qty=0인 보유종목은 holdings에서 제외한다."""
        resp = _fake_kis_balance_response()
        resp["output1"].append({
            "pdno": "000660", "prdt_name": "SK하이닉스",
            "hldg_qty": "0", "ord_psbl_qty": "0",
            "pchs_avg_pric": "150000.0", "prpr": "145000",
            "evlu_pfls_rt": "-3.33",
        })
        result = self._call_get_balance(resp)
        codes = [h["code"] for h in result["holdings"]]
        assert "000660" not in codes, "qty=0 종목은 holdings에 포함되지 않아야 한다"


# ── buy_stock() / sell_stock() 스키마 계약 테스트 ─────────────────────────

class TestOrderSchema:

    def _call_buy(self, api_response, qty=10, price=0):
        mock_resp = MagicMock()
        mock_resp.json.return_value = api_response
        mock_resp.raise_for_status.return_value = None
        mock_resp.status_code = 200

        with patch("mock_trading.kis_client.requests.post", return_value=mock_resp), \
             patch("mock_trading.kis_client.get_token", return_value="dummy_token"), \
             patch("auto_trader.is_nxt_hours", return_value=False):
            from mock_trading.kis_client import buy_stock
            return buy_stock("005930", qty, price)

    def _call_sell(self, api_response, qty=10, price=0):
        mock_resp = MagicMock()
        mock_resp.json.return_value = api_response
        mock_resp.raise_for_status.return_value = None
        mock_resp.status_code = 200

        with patch("mock_trading.kis_client.requests.post", return_value=mock_resp), \
             patch("mock_trading.kis_client.get_token", return_value="dummy_token"), \
             patch("auto_trader.is_nxt_hours", return_value=False):
            from mock_trading.kis_client import sell_stock
            return sell_stock("005930", qty, price)

    def test_매수_성공_스키마(self):
        result = self._call_buy(_fake_kis_order_response(True))
        for key, t in BUY_SELL_SCHEMA.items():
            assert key in result, f"buy_stock 결과에 '{key}' 없음"
            assert isinstance(result[key], t), f"'{key}'는 {t.__name__}여야 함"

    def test_매도_성공_스키마(self):
        result = self._call_sell(_fake_kis_order_response(True))
        for key, t in BUY_SELL_SCHEMA.items():
            assert key in result, f"sell_stock 결과에 '{key}' 없음"
            assert isinstance(result[key], t), f"'{key}'는 {t.__name__}여야 함"

    def test_매수_성공시_success_True(self):
        result = self._call_buy(_fake_kis_order_response(True))
        assert result["success"] is True

    def test_매수_실패시_success_False(self):
        result = self._call_buy(_fake_kis_order_response(False))
        assert result["success"] is False

    def test_매수_성공시_order_no_비지않음(self):
        result = self._call_buy(_fake_kis_order_response(True))
        assert result["order_no"] != "", "성공 시 order_no가 있어야 한다"

    def test_매수_실패시_order_no_빔(self):
        result = self._call_buy(_fake_kis_order_response(False))
        assert result["order_no"] == "", "실패 시 order_no는 빈 문자열이어야 한다"

    def test_HTTP_오류시_success_False(self):
        """HTTP 오류 발생 시 예외 아닌 dict 반환."""
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = Exception("HTTP 500")
        mock_resp.status_code = 500

        with patch("mock_trading.kis_client.requests.post", return_value=mock_resp), \
             patch("mock_trading.kis_client.get_token", return_value="dummy_token"), \
             patch("auto_trader.is_nxt_hours", return_value=False):
            from mock_trading.kis_client import buy_stock
            result = buy_stock("005930", 10, 0)

        assert result["success"] is False, "HTTP 오류 시 success=False여야 한다"
        assert isinstance(result, dict), "HTTP 오류 시에도 dict를 반환해야 한다"


# ── reconcile_holdings 계약 테스트 ───────────────────────────────────────

class TestReconcileHoldingsContract:
    """sync_with_kis의 핵심 로직: DB vs KIS 불일치 분석 결과 스키마."""

    def test_db_에만있는_종목_감지(self):
        from tests.unit.trading_logic import reconcile_holdings
        db = {"005930": {"qty": 10, "avg_price": 70000.0}}
        kis = {}
        to_delete, to_add, to_update = reconcile_holdings(db, kis)
        assert "005930" in to_delete, "KIS에 없는데 DB에 있으면 삭제 대상"

    def test_kis_에만있는_종목_감지(self):
        from tests.unit.trading_logic import reconcile_holdings
        db = {}
        kis = {"000660": {"qty": 5, "avg_price": 150000.0}}
        to_delete, to_add, to_update = reconcile_holdings(db, kis)
        assert any(item["code"] == "000660" for item in to_add), "DB에 없는데 KIS에 있으면 추가 대상"

    def test_수량불일치_감지(self):
        from tests.unit.trading_logic import reconcile_holdings
        db  = {"005930": {"qty": 10, "avg_price": 70000.0}}
        kis = {"005930": {"qty":  7, "avg_price": 70000.0}}
        to_delete, to_add, to_update = reconcile_holdings(db, kis)
        assert any(item["code"] == "005930" for item in to_update), "수량 다르면 업데이트 대상"

    def test_일치시_빈결과(self):
        from tests.unit.trading_logic import reconcile_holdings
        same = {"005930": {"qty": 10, "avg_price": 70000.0}}
        to_delete, to_add, to_update = reconcile_holdings(same, same.copy())
        assert to_delete == []
        assert to_add    == []
        assert to_update == []
