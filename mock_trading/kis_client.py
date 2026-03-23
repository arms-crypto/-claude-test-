#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""KIS API / Yahoo Finance / Naver 주가 조회 + 종목코드 검색"""

import time
import logging
import requests
from bs4 import BeautifulSoup
import yfinance as yf

logger = logging.getLogger(__name__)

# proxy_v53.py와 동일한 KIS 설정
APP_KEY    = "PSY9gMy15uipajb9qM25Cj1Uhf74FVu1cDyF"
APP_SECRET = ("A/vwnErWUmOrZFUoJQ5bBS78WdY1lS6T6GaD5Hx1dNE+J3TTxTi1QwBvdFZuoKHWJ2nKEz+"
              "SaAmZmNikWH04Ge4Mm7up+/5JeAphHOXYld5nIbtehEmHMFcHVeB3EbNQem1pi2+0cVdyj6w7"
              "UzGJA+HqVRNFlPapifykRfPmf4Qf0IaIJdU=")
KIS_URL    = "https://openapivts.koreainvestment.com:443"

_token_cache = {"token": None, "expires_at": 0}


def get_token() -> str:
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"]:
        return _token_cache["token"]
    try:
        r = requests.post(
            f"{KIS_URL}/oauth2/tokenP",
            json={"grant_type": "client_credentials", "appkey": APP_KEY, "appsecret": APP_SECRET},
            timeout=6,
        )
        r.raise_for_status()
        data = r.json()
        token = data.get("access_token")
        if token:
            _token_cache["token"] = token
            _token_cache["expires_at"] = now + int(data.get("expires_in", 43200)) - 60
            return token
    except Exception:
        logger.exception("KIS 토큰 발급 실패")
    return None


def _price_kis(code: str) -> int:
    token = get_token()
    if not token:
        return None
    headers = {
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "FHKST01010400",
    }
    try:
        r = requests.get(
            f"{KIS_URL}/uapi/domestic-stock/v1/quotations/inquire-price",
            params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code},
            headers=headers,
            timeout=8,
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
    try:
        stock = yf.Ticker(f"{code}.KS")
        hist = stock.history(period="1d")
        if not hist.empty:
            return int(hist["Close"].iloc[-1])
        price = stock.info.get("regularMarketPrice") or stock.info.get("previousClose")
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
        )
        soup = BeautifulSoup(r.text, "html.parser")
        tag = soup.select_one("#middle .no_today .blind")
        if tag:
            return int(tag.text.strip().replace(",", ""))
    except Exception:
        pass
    return None


def get_price(code: str) -> int:
    """KIS → Yahoo → Naver 순 폴백. 실패 시 None 반환."""
    return _price_kis(code) or _price_yahoo(code) or _price_naver(code)


def resolve_code(name_or_code: str) -> tuple:
    """
    종목명 또는 6자리 코드 → (code, display_name).
    실패 시 (None, name_or_code) 반환.
    """
    s = name_or_code.strip()
    if len(s) == 6 and s.isdigit():
        return s, s
    # 네이버 금융 검색
    try:
        r = requests.get(
            f"https://finance.naver.com/sise/sise_search.naver?query={s}",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=8,
        )
        soup = BeautifulSoup(r.text, "html.parser")
        link = soup.select_one('a[href*="code="]')
        if link and "code=" in link["href"]:
            code = link["href"].split("code=")[1].split("&")[0]
            display = link.text.strip() or s
            return code, display
    except Exception:
        pass
    return None, s
