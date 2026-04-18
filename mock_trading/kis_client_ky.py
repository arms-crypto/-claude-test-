#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""KIS API 실전 클라이언트 — KY 계좌 (44384407-01)"""

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

APP_KEY    = "PSCO2zW98wAJMzyD4Xpingz0QPPujGQTUyP2"
APP_SECRET = ("HyWiB08P9zRYgeU6WsT43S3rVQhvVXjk6LiL0U3L/LHinBlAbaAzELUqzd9lhGT6DG1VEg8ariiVaOZ"
              "CORxR1SOFBoMjuFYN2vb9J6yYfH5HL1Nqt3xgfJAXGFQAMSJQ6HpkRuxRVrXAJtSETLP5AgmrO2uYp"
              "BFo0LJHb0lGBGsPxcId3RU=")
KIS_URL    = "https://openapi.koreainvestment.com:9443"
ACCOUNT_NO = "44384407"
ACCOUNT_CD = "01"
REAL_TRADE = True  # 실전 주문 활성화

_token_cache = {"token": None, "expires_at": 0, "issued_date": ""}
_token_lock  = threading.Lock()
_TOKEN_FILE  = os.path.join(os.path.dirname(__file__), ".kis_token_cache_ky.json")


def _load_token_from_file():
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
    try:
        with open(_TOKEN_FILE, "w") as f:
            json.dump({"token": _token_cache["token"], "expires_at": _token_cache["expires_at"], "issued_date": _token_cache["issued_date"]}, f)
    except Exception:
        pass


_load_token_from_file()


def get_token() -> str:
    now = time.time()
    today = datetime.now().strftime("%Y-%m-%d")
    if _token_cache["token"] and _token_cache.get("issued_date") == today:
        return _token_cache["token"]
    with _token_lock:
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
            logger.exception("KIS(KY) 토큰 발급 실패")
    return None


def _price_kis(code: str) -> int:
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
        out = r.json().get("output", {})
        if out and out.get("stck_prpr"):
            return int(out["stck_prpr"])
    except Exception:
        logger.exception("KIS(KY) 가격 조회 실패: %s", code)
    return None


def _price_yahoo(code: str) -> int:
    try:
        stock = yf.Ticker(f"{code}.KS")
        hist = stock.history(period="1d")
        if not hist.empty:
            val = hist["Close"].iloc[-1]
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
    """KIS 통합시세 조회 (FID_COND_MRKT_DIV_CODE='UN' — KRX+NXT 통합)."""
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
        logger.debug("KIS(KY) 통합시세 조회 실패: %s", code)
    return None


def get_price(code: str) -> int:
    """KIS KRX → NXT → Naver 순 폴백. 실패 시 None 반환."""
    return _price_kis(code) or get_nxt_price(code) or _price_naver(code)


def get_nxt_price(code: str) -> int:
    """NXT 야간 시세 조회. 실패 시 None 반환."""
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
        logger.debug("KIS(KY) NXT 가격 조회 실패: %s", code)
    return None


def get_best_price(code: str) -> int:
    """KRX → NXT → Naver 순 폴백."""
    return _price_kis(code) or get_nxt_price(code) or _price_naver(code)


def get_current_price(code: str) -> int:
    """통합시세 우선 현재가 조회.
    1순위: KIS 통합시세 (UN — KRX+NXT 자동 선택)
    2순위: KRX / 3순위: NXT / 4순위: Naver
    """
    return _price_unified(code) or _price_kis(code) or get_nxt_price(code) or _price_naver(code)


_nxt_support_cache: dict = {}

def is_nxt_supported(code: str) -> bool:
    """NXT 지원 여부 확인. 캐시 적용."""
    if code in _nxt_support_cache:
        return _nxt_support_cache[code]
    result = get_nxt_price(code) is not None
    _nxt_support_cache[code] = result
    return result


def _order_headers(tr_id: str) -> dict:
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
            "ORD_DVSN":     "01",   # 시장가
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
            logger.info("KIS(KY) 주문가능금액 %s: %d원", code, amt)
            return amt
        logger.warning("KIS(KY) 주문가능금액 조회 실패 %s: %s", code, data.get("msg1", ""))
        return 0
    except Exception:
        logger.exception("KIS(KY) 주문가능금액 조회 예외: %s", code)
        return 0


def buy_stock(code: str, qty: int, price: int = 0) -> dict:
    """
    매수 주문 — REAL_TRADE=True, 실제 주문 전송.
    - 정규장 (09:00~15:30): TTTC0802U
    - NXT 시간 (08:00~09:00, 15:30~20:00): TTTT0802U (NXT 지원 종목만)
    price=0 → 시장가, price>0 → 지정가
    """
    from auto_trader import is_nxt_hours as _is_nxt_hours
    nxt_time = _is_nxt_hours()

    if nxt_time and not is_nxt_supported(code):
        logger.warning("KIS(KY) NXT 시간 KRX전용 종목 매수 차단: %s", code)
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
            logger.info("KIS(KY) 실전 매수 완료 %s %d주 [%s] 주문번호:%s", code, qty, tr_id, order_no)
            return {"success": True, "order_no": order_no, "msg": data.get("msg1", "")}
        else:
            logger.error("KIS(KY) 실전 매수 실패 %s: %s", code, data.get("msg1", ""))
            return {"success": False, "order_no": "", "msg": data.get("msg1", "")}
    except Exception:
        logger.exception("KIS(KY) 실전 매수 예외: %s", code)
        return {"success": False, "order_no": "", "msg": "API 오류"}


def sell_stock(code: str, qty: int, price: int = 0) -> dict:
    """
    매도 주문 — REAL_TRADE=True, 실제 주문 전송.
    - 정규장 (09:00~15:30): TTTC0801U
    - NXT 시간 (08:00~09:00, 15:30~20:00): TTTT0801U
    price=0 → 시장가, price>0 → 지정가
    """
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
            logger.info("KIS(KY) 실전 매도 완료 %s %d주 [%s] 주문번호:%s", code, qty, tr_id, order_no)
            return {"success": True, "order_no": order_no, "msg": data.get("msg1", "")}
        else:
            logger.error("KIS(KY) 실전 매도 실패 %s: %s", code, data.get("msg1", ""))
            return {"success": False, "order_no": "", "msg": data.get("msg1", "")}
    except Exception:
        logger.exception("KIS(KY) 실전 매도 예외: %s", code)
        return {"success": False, "order_no": "", "msg": "API 오류"}


def get_balance() -> dict:
    """
    KIS 실전 잔고 조회.
    반환: {"cash": int, "holdings": [{"code", "name", "qty", "avg_price", "current_price", "pnl"}]}
    """
    token = get_token()
    if not token:
        return {"cash": 0, "holdings": []}
    try:
        r = requests.get(
            f"{KIS_URL}/uapi/domestic-stock/v1/trading/inquire-balance",
            params={
                "CANO":                 ACCOUNT_NO,
                "ACNT_PRDT_CD":         ACCOUNT_CD,
                "AFHR_FLPR_YN":         "N",
                "OFL_YN":               "N",
                "INQR_DVSN":            "02",
                "UNPR_DVSN":            "01",
                "FUND_STTL_ICLD_YN":    "N",
                "FNCG_AMT_AUTO_RDPT_YN":"N",
                "PRCS_DVSN":            "00",
                "CTX_AREA_FK100":       "",
                "CTX_AREA_NK100":       "",
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
        logger.exception("KIS(KY) 잔고 조회 실패")
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
            df = _px.get_market_ohlcv_by_date(from_date, today, code, freq="W")
        else:  # M
            df = _px.get_market_ohlcv_by_date(from_date, today, code, freq="M")
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
        logger.warning("pykrx(KY) OHLCV 폴백 실패: %s %s — %s", code, period, e)
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
        "FID_INPUT_ISCD":         code,
        "FID_INPUT_DATE_1":       from_date,
        "FID_INPUT_DATE_2":       today,
        "FID_PERIOD_DIV_CODE":    period,
        "FID_ORG_ADJ_PRC":        "0",
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
        result.reverse()
        return result[-count:]
    except Exception:
        logger.warning("KIS(KY) OHLCV 조회 실패: %s %s — pykrx 폴백 시도", code, period)
        return _get_ohlcv_pykrx(code, period, count)


def get_minute_ohlcv(code: str, interval: int = 1, count: int = 60) -> list:
    """
    KIS API 분봉 데이터 조회.
    interval: 1=1분봉, 15/30/60분은 리샘플
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
        "FID_ETC_CLS_CODE":       "",
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD":         code,
        "FID_INPUT_HOUR_1":       "000000",
        "FID_PW_DATA_INCU_YN":    "Y",
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
        rows.reverse()

        if interval > 1 and rows:
            import pandas as pd
            df = pd.DataFrame(rows)
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
        logger.exception("KIS(KY) 분봉 조회 실패: %s", code)
        return []


# ────────────────────────────────────────────────────────────────────────
# 코드 해석 함수 (kis_client.py와 동일)
# ────────────────────────────────────────────────────────────────────────

def _name_by_pykrx(code: str) -> str:
    """pykrx로 단일 종목명 조회."""
    try:
        from pykrx import stock as _px
        return _px.get_market_ticker_name(code)
    except Exception:
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
