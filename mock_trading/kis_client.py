#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -*- coding: utf-8 -*-
"""KIS API / Yahoo Finance / Naver 주가 조회 + 종목코드 검색"""

from datetime import datetime
import os
import json
import time
import threading
import logging
import requests
from bs4 import BeautifulSoup
import yfinance as yf

logger = logging.getLogger(__name__)

# KIS 실전 설정 (데이터 조회용 — 실제 주문은 REAL_TRADE=True 시만 허용)
APP_KEY    = "PSLX9xi6Y1FLm2QvO7aqnTKWQJfUtwgejebj"
APP_SECRET = ("K2c8EHjkcW56qvDYzNHGAtnzGNsVcGCFurssgTKYSVJF6tN8yueG0kfDLOiwyTdcRZkoYTWtYk1"
              "YeQ8PDehOL3JoJZdBg+95i6MS7lHvo8lDJjL2JIFPqFWpSQm8fbq1QZQddCmMsScaMzzLxHa3"
              "jw3RaBeb5aG9T7yGKfhBNwzAvOA3ayY=")
KIS_URL    = "https://openapi.koreainvestment.com:9443"  # 실전
ACCOUNT_NO = "44197559"   # 실전 계좌번호
ACCOUNT_CD = "01"         # 계좌상품코드 (주식)
REAL_TRADE = True         # 실전 매매 활성화

_token_cache = {"token": None, "expires_at": 0, "issued_date": ""}
_token_lock  = threading.Lock()   # 동시 토큰 발급 방지
_TOKEN_FILE  = os.path.join(os.path.dirname(__file__), ".kis_token_cache.json")


def _load_token_from_file():
    """재시작 후 파일에서 토큰 복구 — KIS 하루 1회 발급 제한 대응"""
    try:
        with open(_TOKEN_FILE) as f:
            data = json.load(f)
        if data.get("token") and data.get("expires_at", 0) > time.time() + 60:
            _token_cache["token"] = data["token"]
            _token_cache["expires_at"] = data["expires_at"]
            _token_cache["issued_date"] = data.get("issued_date", "")
    except (FileNotFoundError, Exception):
        pass


def _save_token_to_file():
    """KIS 토큰을 파일에 캐시 저장. 서버 재시작 후 재발급 없이 재사용."""
    try:
        with open(_TOKEN_FILE, "w") as f:
            json.dump({"token": _token_cache["token"], "expires_at": _token_cache["expires_at"], "issued_date": _token_cache["issued_date"]}, f)
    except Exception:
        pass


_load_token_from_file()  # 모듈 로드 시 파일에서 복구


def get_token() -> str:
    """KIS OAuth2 토큰 발급/캐시 조회. 캐시 유효하면 즉시 반환, 만료 시 재발급.

    Returns:
        str: KIS OAuth2 토큰, 발급 실패 시 None
    """
    now = time.time()
    today = datetime.now().strftime("%Y-%m-%d")
    # 락 없이 먼저 캐시 확인 (빠른 경로)
    if _token_cache["token"] and _token_cache.get("issued_date") == today:
        return _token_cache["token"]
    with _token_lock:
        # 락 획득 후 다시 확인 (다른 스레드가 이미 발급했을 수 있음)
        now = time.time()
        today = datetime.now().strftime("%Y-%m-%d")
        if _token_cache["token"] and _token_cache.get("issued_date") == today:
            return _token_cache["token"]
        try:
            r = requests.post(
                f"{KIS_URL}/oauth2/tokenP",
                json={"grant_type": "client_credentials", "appkey": APP_KEY, "appsecret": APP_SECRET},
                timeout=6,
                proxies={"http": None, "https": None},
            )
            r.raise_for_status()
            data = r.json()
            token = data.get("access_token")
            if token:
                _token_cache["token"] = token
                _token_cache["expires_at"] = now + int(data.get("expires_in", 86400)) - 60
                _token_cache["issued_date"] = datetime.now().strftime("%Y-%m-%d")
                _save_token_to_file()
                return token
        except Exception:
            logger.exception("KIS 토큰 발급 실패")
    return None


def _price_kis(code: str) -> int:
    """KIS API로 현재 주가 조회 (KRX 종목).

    Args:
        code (str): 종목코드 (6자리)

    Returns:
        int: 현재가, 조회 실패 시 None
    """
    token = get_token()
    if not token:
        return None
    headers = {
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "FHKST01010100",
    }
    try:
        r = requests.get(
            f"{KIS_URL}/uapi/domestic-stock/v1/quotations/inquire-price",
            params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code},
            headers=headers,
            timeout=8,
            proxies={"http": None, "https": None},
        )
        r.raise_for_status()
        data = r.json()
        out = data.get("output", {})
        if out and out.get("stck_prpr"):
            return int(out["stck_prpr"])
    except Exception:
        logger.exception("KIS 가격 조회 실패: %s", code)
    return None


def _price_yahoo(code: str) -> int:
    """yfinance로 한국 주식 현재가 조회 (Yahoo Finance).

    Args:
        code (str): 종목코드 (6자리)

    Returns:
        int: 현재가, 조회 실패 시 None
    """
    try:
        stock = yf.Ticker(f"{code}.KS")
        hist = stock.history(period="1d")
        if not hist.empty:
            val = hist["Close"].iloc[-1]
            # yfinance 버전에 따라 Series 반환 가능
            if hasattr(val, "iloc"):
                val = val.iloc[0]
            return int(float(val))
        price = stock.fast_info.get("lastPrice") or stock.info.get("regularMarketPrice") or stock.info.get("previousClose")
        if price:
            return int(price)
    except Exception:
        pass
    return None


def _price_naver(code: str) -> int:
    """네이버 금융에서 한국 주식 현재가 조회 (웹 크롤링).

    Args:
        code (str): 종목코드 (6자리)

    Returns:
        int: 현재가, 조회 실패 시 None
    """
    try:
        r = requests.get(
            f"https://finance.naver.com/item/main.naver?code={code}",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=8,
            proxies={"http": None, "https": None},
        )
        soup = BeautifulSoup(r.text, "html.parser")
        tag = soup.select_one("#middle .no_today .blind") or soup.select_one(".no_today em.blind")
        if tag:
            return int(tag.text.strip().replace(",", ""))
    except Exception:
        pass
    return None


def _price_unified(code: str) -> int:
    """KIS 통합시세 조회 (FID_COND_MRKT_DIV_CODE='UN' — KRX+NXT 통합).
    정규장/비정규장 구분 없이 현재 체결가 반환. 실패 시 None.
    """
    token = get_token()
    if not token:
        return None
    headers = {
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "FHKST01010100",
    }
    try:
        r = requests.get(
            f"{KIS_URL}/uapi/domestic-stock/v1/quotations/inquire-price",
            params={"FID_COND_MRKT_DIV_CODE": "UN", "FID_INPUT_ISCD": code},
            headers=headers,
            timeout=8,
            proxies={"http": None, "https": None},
        )
        r.raise_for_status()
        out = r.json().get("output", {})
        if out and out.get("stck_prpr"):
            return int(out["stck_prpr"])
    except Exception:
        logger.debug("KIS 통합시세 조회 실패: %s", code)
    return None


def get_price(code: str) -> int:
    """KIS KRX → NXT → Naver 순 폴백. 실패 시 None 반환."""
    return _price_kis(code) or get_nxt_price(code) or _price_naver(code)


def get_nxt_price(code: str) -> int:
    """
    넥스트트레이드(NXT) 야간 시세 조회.
    장 마감 후(15:30~) NXT 거래 중일 때 사용. 실패 시 None 반환.
    """
    token = get_token()
    if not token:
        return None
    headers = {
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "FHKST01010100",
    }
    try:
        r = requests.get(
            f"{KIS_URL}/uapi/domestic-stock/v1/quotations/inquire-price",
            params={"FID_COND_MRKT_DIV_CODE": "NX", "FID_INPUT_ISCD": code},
            headers=headers,
            timeout=8,
            proxies={"http": None, "https": None},
        )
        r.raise_for_status()
        out = r.json().get("output", {})
        if out and out.get("stck_prpr"):
            return int(out["stck_prpr"])
    except Exception:
        logger.debug("NXT 가격 조회 실패: %s", code)
    return None


def get_best_price(code: str) -> int:
    """KRX → NXT → Naver 순 폴백."""
    return _price_kis(code) or get_nxt_price(code) or _price_naver(code)


def get_current_price(code: str) -> int:
    """통합시세 우선 현재가 조회.
    1순위: KIS 통합시세 (UN — KRX+NXT 자동 선택, 장중/비정규장 무관)
    2순위: KRX 시세
    3순위: NXT 시세
    4순위: Naver 금융
    """
    return _price_unified(code) or _price_kis(code) or get_nxt_price(code) or _price_naver(code)


# NXT 지원 여부 캐시 (종목코드 → True/False)
_nxt_support_cache: dict = {}

def is_nxt_supported(code: str) -> bool:
    """종목이 NXT(넥스트트레이드) 지원 여부 확인. 캐시 적용."""
    if code in _nxt_support_cache:
        return _nxt_support_cache[code]
    result = get_nxt_price(code) is not None
    _nxt_support_cache[code] = result
    logger.debug("NXT 지원 %s: %s", code, result)
    return result


def get_orderbook(code: str) -> dict:
    """호가창 조회 — 매수/매도 1~5호가 잔량 반환 (FHKST01010200).
    반환: {ask_price, ask_qty, bid_price, bid_qty, ask_total, bid_total}
    실패 시 {}
    """
    token = get_token()
    if not token:
        return {}
    headers = {
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "FHKST01010200",
    }
    try:
        r = requests.get(
            f"{KIS_URL}/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn",
            params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code},
            headers=headers,
            timeout=5,
            proxies={"http": None, "https": None},
        )
        r.raise_for_status()
        data = r.json()
        out = data.get("output1", {})
        if not out:
            return {}
        ask_price = [int(out.get(f"askp{i}", 0)) for i in range(1, 6)]
        ask_qty   = [int(out.get(f"askp_rsqn{i}", 0)) for i in range(1, 6)]
        bid_price = [int(out.get(f"bidp{i}", 0)) for i in range(1, 6)]
        bid_qty   = [int(out.get(f"bidp_rsqn{i}", 0)) for i in range(1, 6)]
        ask_total = int(out.get("total_askp_rsqn", 0))
        bid_total = int(out.get("total_bidp_rsqn", 0))
        logger.info("호가 %s 매도총잔량:%d 매수총잔량:%d 1호가매도:%d", code, ask_total, bid_total, ask_price[0])
        return {"ask_price": ask_price, "ask_qty": ask_qty,
                "bid_price": bid_price, "bid_qty": bid_qty,
                "ask_total": ask_total, "bid_total": bid_total}
    except Exception as e:
        logger.warning("호가 조회 실패 %s: %s", code, e)
        return {}



def _order_headers(tr_id: str) -> dict:
    """KIS 주문 헤더 생성."""
    token = get_token()
    return {
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": tr_id,
        "content-type": "application/json; charset=utf-8",
    }


def get_available_amount(code: str, price: int = 0) -> int:
    """KIS API로 실제 주문가능금액 조회 (TTTC8908R).
    반환: 주문가능현금(원), 실패 시 0
    """
    try:
        params = {
            "CANO":         ACCOUNT_NO,
            "ACNT_PRDT_CD": ACCOUNT_CD,
            "PDNO":         code,
            "ORD_UNPR":     str(price) if price > 0 else "0",
            "ORD_DVSN":     "01",
            "CMA_EVLU_AMT_ICLD_YN": "N",
            "OVRS_ICLD_YN": "N",
        }
        r = requests.get(
            f"{KIS_URL}/uapi/domestic-stock/v1/trading/inquire-psbl-order",
            params=params,
            headers=_order_headers("TTTC8908R"),
            timeout=5,
            proxies={"http": None, "https": None},
        )
        r.raise_for_status()
        data = r.json()
        if data.get("rt_cd") == "0":
            amt = int(data.get("output", {}).get("ord_psbl_cash", 0))
            logger.info("KIS 주문가능금액 %s: %d원", code, amt)
            return amt
        logger.warning("KIS 주문가능금액 조회 실패 %s: %s", code, data.get("msg1", ""))
        return 0
    except Exception:
        logger.exception("KIS 주문가능금액 조회 예외: %s", code)
        return 0


def buy_stock(code: str, qty: int, price: int = 0) -> dict:
    """매수 주문. price=0 시장가, price>0 지정가."""
    from auto_trader import is_nxt_hours as _is_nxt_hours
    nxt_time = _is_nxt_hours()

    if nxt_time and not is_nxt_supported(code):
        logger.warning("NXT 시간 KRX전용 종목 매수 차단: %s", code)
        return {"success": False, "order_no": "", "msg": "NXT 미지원 종목 — KRX 시간에만 거래 가능"}

    tr_id = "TTTT0802U" if (nxt_time and is_nxt_supported(code)) else "TTTC0802U"

    try:
        body = {
            "CANO":           ACCOUNT_NO,
            "ACNT_PRDT_CD":   ACCOUNT_CD,
            "PDNO":           code,
            "ORD_DVSN":       "01" if price == 0 else "00",
            "ORD_QTY":        str(qty),
            "ORD_UNPR":       "0" if price == 0 else str(price),
        }
        for _attempt in range(2):
            r = requests.post(
                f"{KIS_URL}/uapi/domestic-stock/v1/trading/order-cash",
                json=body,
                headers=_order_headers(tr_id),
                timeout=10,
                proxies={"http": None, "https": None},
            )
            if r.status_code == 500 and _attempt == 0:
                import time as _time
                logger.warning("KIS 매수 500 에러 %s — 3초 후 재시도", code)
                _time.sleep(3)
                continue
            r.raise_for_status()
            break
        data = r.json()
        if data.get("rt_cd") == "0":
            order_no = data.get("output", {}).get("ODNO", "")
            logger.info("KIS 실전 매수 완료 %s %d주 [%s] 주문번호:%s", code, qty, tr_id, order_no)
            return {"success": True, "order_no": order_no, "msg": data.get("msg1", "")}
        else:
            logger.error("KIS 실전 매수 실패 %s: %s", code, data.get("msg1", ""))
            return {"success": False, "order_no": "", "msg": data.get("msg1", "")}
    except requests.exceptions.HTTPError as e:
        logger.warning("KIS 실전 매수 HTTP오류 %s: %s", code, e)
        return {"success": False, "order_no": "", "msg": f"HTTP오류 {e.response.status_code}"}
    except Exception:
        logger.exception("KIS 실전 매수 예외: %s", code)
        return {"success": False, "order_no": "", "msg": "API 오류"}


def sell_stock(code: str, qty: int, price: int = 0) -> dict:
    """매도 주문. price=0 시장가, price>0 지정가."""
    from auto_trader import is_nxt_hours as _is_nxt_hours
    tr_id = "TTTT0801U" if (_is_nxt_hours() and is_nxt_supported(code)) else "TTTC0801U"

    try:
        body = {
            "CANO":           ACCOUNT_NO,
            "ACNT_PRDT_CD":   ACCOUNT_CD,
            "PDNO":           code,
            "ORD_DVSN":       "01" if price == 0 else "00",
            "ORD_QTY":        str(qty),
            "ORD_UNPR":       "0" if price == 0 else str(price),
        }
        r = requests.post(
            f"{KIS_URL}/uapi/domestic-stock/v1/trading/order-cash",
            json=body,
            headers=_order_headers(tr_id),
            timeout=10,
            proxies={"http": None, "https": None},
        )
        r.raise_for_status()
        data = r.json()
        if data.get("rt_cd") == "0":
            order_no = data.get("output", {}).get("ODNO", "")
            logger.info("KIS 실전 매도 완료 %s %d주 [%s] 주문번호:%s", code, qty, tr_id, order_no)
            return {"success": True, "order_no": order_no, "msg": data.get("msg1", "")}
        else:
            logger.error("KIS 실전 매도 실패 %s: %s", code, data.get("msg1", ""))
            return {"success": False, "order_no": "", "msg": data.get("msg1", "")}
    except Exception:
        logger.exception("KIS 실전 매도 예외: %s", code)
        return {"success": False, "order_no": "", "msg": "API 오류"}


def get_balance() -> dict:
    """
    KIS 모의투자 잔고 조회.
    반환: {"cash": int, "holdings": [{"code", "name", "qty", "avg_price", "current_price", "pnl"}]}
    """
    token = get_token()
    if not token:
        return {"cash": 0, "holdings": []}
    try:
        r = requests.get(
            f"{KIS_URL}/uapi/domestic-stock/v1/trading/inquire-balance",
            params={
                "CANO": ACCOUNT_NO,
                "ACNT_PRDT_CD": ACCOUNT_CD,
                "AFHR_FLPR_YN": "N",
                "OFL_YN": "N",
                "INQR_DVSN": "02",
                "UNPR_DVSN": "01",
                "FUND_STTL_ICLD_YN": "N",
                "FNCG_AMT_AUTO_RDPT_YN": "N",
                "PRCS_DVSN": "00",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
            },
            headers=_order_headers("TTTC8434R"),
            timeout=10,
            proxies={"http": None, "https": None},
        )
        r.raise_for_status()
        data = r.json()
        output2 = data.get("output2", [{}])
        cash = int(output2[0].get("dnca_tot_amt", 0)) if output2 else 0
        holdings = []
        for item in data.get("output1", []):
            qty = int(item.get("hldg_qty", 0))
            if qty <= 0:
                continue
            holdings.append({
                "code":          item.get("pdno", ""),
                "name":          item.get("prdt_name", ""),
                "qty":           qty,
                "avg_price":     float(item.get("pchs_avg_pric", 0)),
                "current_price": int(item.get("prpr", 0)),
                "pnl":           float(item.get("evlu_pfls_rt", 0)),
            })
        return {"cash": cash, "holdings": holdings}
    except Exception:
        logger.exception("KIS 잔고 조회 실패")
        return {"cash": 0, "holdings": []}


def _get_ohlcv_pykrx(code: str, period: str = "D", count: int = 60) -> list:
    """pykrx로 OHLCV 조회 (KIS 레이트리밋 초과 시 폴백)."""
    try:
        import datetime
        from pykrx import stock as _px
        today = datetime.date.today().strftime("%Y%m%d")
        days_back = {"D": count * 2, "W": count * 10, "M": count * 35}.get(period, count * 2)
        from_date = (datetime.date.today() - datetime.timedelta(days=days_back)).strftime("%Y%m%d")
        if period == "D":
            df = _px.get_market_ohlcv_by_date(from_date, today, code)
        elif period == "W":
            df = _px.get_market_ohlcv_by_date(from_date, today, code, freq="w")
        else:  # M
            df = _px.get_market_ohlcv_by_date(from_date, today, code, freq="m")
        if df is None or df.empty:
            return []
        result = []
        for idx, row in df.iterrows():
            try:
                result.append({
                    "date":   idx.strftime("%Y%m%d"),
                    "open":   int(row.get("시가", row.get("Open", 0))),
                    "high":   int(row.get("고가", row.get("High", 0))),
                    "low":    int(row.get("저가", row.get("Low", 0))),
                    "close":  int(row.get("종가", row.get("Close", 0))),
                    "volume": int(row.get("거래량", row.get("Volume", 0))),
                })
            except Exception:
                pass
        return result[-count:]
    except Exception as e:
        logger.warning("pykrx OHLCV 폴백 실패: %s %s — %s", code, period, e)
        return []


def get_ohlcv(code: str, period: str = "D", count: int = 60) -> list:
    """
    KIS API 차트 데이터 조회.
    period: D=일봉, W=주봉, M=월봉
    반환: [{"date","open","high","low","close","volume"}, ...] 오래된 것부터
    """
    import datetime
    token = get_token()
    if not token:
        return []
    today = datetime.date.today().strftime("%Y%m%d")
    # 충분히 과거부터 조회 (count봉 확보)
    days_back = {"D": count * 2, "W": count * 10, "M": count * 35}.get(period, count * 2)
    from_date = (datetime.date.today() - datetime.timedelta(days=days_back)).strftime("%Y%m%d")
    headers = {
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "FHKST03010100",
    }
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": code,
        "FID_INPUT_DATE_1": from_date,
        "FID_INPUT_DATE_2": today,
        "FID_PERIOD_DIV_CODE": period,
        "FID_ORG_ADJ_PRC": "0",
    }
    try:
        r = requests.get(
            f"{KIS_URL}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
            params=params, headers=headers, timeout=10,
            proxies={"http": None, "https": None},
        )
        r.raise_for_status()
        output = r.json().get("output2", [])
        result = []
        for row in output:
            try:
                result.append({
                    "date":   row.get("stck_bsop_date", ""),
                    "open":   int(row.get("stck_oprc", 0)),
                    "high":   int(row.get("stck_hgpr", 0)),
                    "low":    int(row.get("stck_lwpr", 0)),
                    "close":  int(row.get("stck_clpr", 0)),
                    "volume": int(row.get("acml_vol", 0)),
                })
            except Exception:
                pass
        result.reverse()  # 오래된 것부터
        return result[-count:]
    except Exception:
        logger.warning("KIS OHLCV 조회 실패: %s %s — pykrx 폴백 시도", code, period)
        return _get_ohlcv_pykrx(code, period, count)


def get_minute_ohlcv(code: str, interval: int = 1, count: int = 60) -> list:
    """
    KIS API 분봉 데이터 조회.
    interval: 1=1분봉 원시데이터 (15/30/60분은 호출 후 직접 리샘플)
    반환: [{"time","open","high","low","close","volume"}, ...] 오래된 것부터
    """
    token = get_token()
    if not token:
        return []
    headers = {
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "FHKST03010200",
    }
    params = {
        "FID_ETC_CLS_CODE": "",
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": code,
        "FID_INPUT_HOUR_1": "000000",
        "FID_PW_DATA_INCU_YN": "Y",
    }
    try:
        r = requests.get(
            f"{KIS_URL}/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice",
            params=params, headers=headers, timeout=10,
            proxies={"http": None, "https": None},
        )
        r.raise_for_status()
        output = r.json().get("output2", [])
        rows = []
        for row in output:
            try:
                rows.append({
                    "time":   row.get("stck_cntg_hour", ""),
                    "open":   int(row.get("stck_oprc", 0)),
                    "high":   int(row.get("stck_hgpr", 0)),
                    "low":    int(row.get("stck_lwpr", 0)),
                    "close":  int(row.get("stck_prpr", 0)),
                    "volume": int(row.get("cntg_vol", 0)),
                })
            except Exception:
                pass
        rows.reverse()  # 오래된 것부터

        # interval 리샘플 (15/30/60분)
        if interval > 1 and rows:
            import pandas as pd
            df = pd.DataFrame(rows)
            df.index = range(len(df))
            resampled = []
            for i in range(0, len(df), interval):
                chunk = df.iloc[i:i + interval]
                if chunk.empty:
                    continue
                resampled.append({
                    "time":   chunk.iloc[0]["time"],
                    "open":   int(chunk.iloc[0]["open"]),
                    "high":   int(chunk["high"].max()),
                    "low":    int(chunk["low"].min()),
                    "close":  int(chunk.iloc[-1]["close"]),
                    "volume": int(chunk["volume"].sum()),
                })
            return resampled[-count:]

        return rows[-count:]
    except Exception:
        logger.exception("KIS 분봉 조회 실패: %s", code)
        return []


def _name_by_pykrx(code: str) -> str:
    """pykrx로 종목코드 → 종목명 (폴백용)."""
    try:
        from pykrx import stock as _px
        name = _px.get_market_ticker_name(code)
        if name:
            return name
    except Exception:
        pass
    return None


def _code_by_pykrx(name: str) -> tuple:
    """pykrx로 종목명 → (code, name) (폴백용). 전체 스캔이라 느림."""
    try:
        from pykrx import stock as _px
        import datetime as _dt
        today = _dt.date.today().strftime("%Y%m%d")
        for market in ("KOSPI", "KOSDAQ"):
            try:
                ticker_list = _px.get_market_ticker_list(today, market=market)
                if not ticker_list:
                    logger.warning(f"pykrx: {market} 종목 리스트 공 (API 비정상)")
                    continue
                for code in ticker_list:
                    try:
                        n = _px.get_market_ticker_name(code)
                        if n and name in n:
                            return code, n
                    except Exception as e:
                        # 개별 종목 조회 실패는 무시하고 계속
                        pass
            except (requests.exceptions.JSONDecodeError, Exception) as e:
                logger.warning(f"pykrx {market} 조회 실패: {type(e).__name__} — 다음 마켓 시도")
                continue
    except Exception as e:
        logger.warning(f"pykrx 폴백 실패: {e}")
    return None, None


def resolve_code(name_or_code: str) -> tuple:
    """
    종목명 또는 6자리 코드 → (code, display_name).
    Naver 실패 시 pykrx 폴백.
    실패 시 (None, name_or_code) 반환.
    """
    s = name_or_code.strip()
    if len(s) == 6 and s.isdigit():
        # 1차: 네이버 금융에서 종목명 조회
        try:
            r = requests.get(
                f"https://finance.naver.com/item/main.naver?code={s}",
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=8,
                proxies={"http": None, "https": None},
            )
            from bs4 import BeautifulSoup as _BS
            soup = _BS(r.text, "html.parser")
            title = soup.select_one(".wrap_company h2 a")
            if title and title.text.strip():
                return s, title.text.strip()
        except Exception:
            pass
        # 2차 폴백: pykrx
        name = _name_by_pykrx(s)
        return s, (name or s)

    # 종목명 → 코드: 1차 네이버 검색
    try:
        r = requests.get(
            f"https://search.naver.com/search.naver?query={s}+주가",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=8,
            proxies={"http": None, "https": None},
        )
        soup = BeautifulSoup(r.text, "html.parser")
        link = soup.select_one('a[href*="finance.naver.com/item/main"]')
        if link and "code=" in link["href"]:
            code = link["href"].split("code=")[1].split("&")[0]
            try:
                rn = requests.get(
                    f"https://finance.naver.com/item/main.naver?code={code}",
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=8,
                    proxies={"http": None, "https": None},
                )
                sn = BeautifulSoup(rn.text, "html.parser")
                title = sn.select_one(".wrap_company h2 a")
                display = title.text.strip() if (title and title.text.strip()) else s
            except Exception:
                display = s
            return code, display
    except Exception:
        pass
    # 2차 폴백: pykrx 전체 스캔
    code, name = _code_by_pykrx(s)
    if code:
        return code, name
    return None, s
