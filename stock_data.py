#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
stock_data.py — 주가 조회 + 순매수 데이터
get_hantu_token(), get_yahoo_price(), get_price_by_code(), get_naver_price(),
naver_search_code(), stock_price_overseas(), korea_invest_stock(),
naver_news(), _naver_net_buy_list(), get_foreign_net_buy(),
_get_today_institutional_net_buy()
"""

import re
import time
import datetime
import logging
import requests
from bs4 import BeautifulSoup
import yfinance as yf
import pandas as pd
import pytz

import config
from db_utils import (
    get_db_pool, get_stock_code_from_db, save_stock_code_to_db
)

logger = config.logger


# -------------------------
# 토큰 발급 유틸
def get_hantu_token():
    current_time = time.time()
    if config.hantu_token_cache["token"] and current_time < config.hantu_token_cache["expires_at"]:
        return config.hantu_token_cache["token"]
    try:
        auth_url = f"{config.URL}/oauth2/tokenP"
        body = {"grant_type": "client_credentials", "appkey": config.APP_KEY, "appsecret": config.APP_SECRET}
        r = requests.post(auth_url, json=body, timeout=6)
        r.raise_for_status()
        data = r.json()
        token = data.get("access_token")
        expires_in = int(data.get("expires_in", 12 * 60 * 60))
        if token:
            config.hantu_token_cache["token"] = token
            config.hantu_token_cache["expires_at"] = current_time + expires_in - 60
            logger.info("한투 토큰 발급 성공")
            return token
        logger.warning("토큰 응답에 access_token 없음: %s", data)
    except Exception:
        logger.exception("get_hantu_token 예외")
    return None


# -------------------------
# 가격 조회 헬퍼들
def get_yahoo_price(code: str) -> str:
    """KRX 코드(예: 005930) -> '82,300원' 형식 반환"""
    try:
        ticker = f"{code}.KS"
        stock = yf.Ticker(ticker)
        hist = stock.history(period="1d")
        if not hist.empty:
            price = hist["Close"].iloc[-1]
            return f"{int(price):,}원"
        price = stock.info.get('regularMarketPrice') or stock.info.get('previousClose')
        if price:
            return f"{int(price):,}원"
    except Exception:
        logger.exception("get_yahoo_price 실패")
    return None


def get_price_by_code(code: str) -> str:
    """KIS API 호출로 가격 조회 (code는 6자리)"""
    token = get_hantu_token()
    if not token:
        logger.warning("토큰 없음: get_price_by_code 실패")
        return None
    headers = {
        "authorization": f"Bearer {token}",
        "appkey": config.APP_KEY,
        "appsecret": config.APP_SECRET,
        "tr_id": "FHKST01010400"
    }
    params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code}
    try:
        r = requests.get(f"{config.URL}/uapi/domestic-stock/v1/quotations/inquire-price", params=params, headers=headers, timeout=8)
        r.raise_for_status()
        data = r.json()
        if 'output' in data and data['output'] and 'stck_prpr' in data['output']:
            price = data['output']['stck_prpr']
            return f"{int(price):,}원"
        logger.debug("KIS 응답: %s", data)
    except Exception:
        logger.exception("get_price_by_code 예외")
    return None


def get_naver_price(code: str) -> str:
    """네이버 스크래핑으로 가격 조회 (code는 6자리)"""
    try:
        url = f"https://finance.naver.com/item/main.naver?code={code}"
        r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=8)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, 'html.parser')
        price_tag = soup.select_one('#middle .no_today .blind')
        if price_tag:
            return price_tag.text.strip()
    except Exception:
        logger.exception("get_naver_price 예외")
    return None


def naver_search_code(query: str) -> str:
    """종목명으로 코드 조회 — kis_client.resolve_code 사용"""
    try:
        from mock_trading.kis_client import resolve_code
        clean = re.sub(
            r'(주가|현재가|가격|시세|얼마야|얼마에요|얼마임|알려줘요|알려줘|조회해줘|조회|지금|어때요|어때|\?|！|!)',
            '', query
        ).strip().rstrip('요은는이가도좀')
        code, name = resolve_code(clean)
        if code:
            logger.info("naver_search_code: '%s' → %s(%s)", clean, name, code)
        return code
    except Exception:
        logger.exception("naver_search_code 예외")
    return None


# -------------------------
# 해외 주가: 한국어 이름 → 티커 매핑 + yfinance Search 폴백
_OVERSEAS_MAP = {
    # 미국 빅테크
    "엔비디아": "NVDA", "테슬라": "TSLA", "애플": "AAPL", "아마존": "AMZN",
    "마이크로소프트": "MSFT", "구글": "GOOGL", "알파벳": "GOOGL", "메타": "META",
    "넷플릭스": "NFLX", "인텔": "INTC", "AMD": "AMD", "퀄컴": "QCOM",
    # 인기 종목
    "팔란티어": "PLTR", "스페이스엑스": "SPCE", "버진갤럭틱": "SPCE",
    "코인베이스": "COIN", "로빈후드": "HOOD", "리비안": "RIVN", "루시드": "LCID",
    "니오": "NIO", "바이두": "BIDU", "알리바바": "BABA", "텐센트": "TCEHY",
    "줌": "ZM", "스포티파이": "SPOT", "우버": "UBER", "에어비앤비": "ABNB",
    "쇼피파이": "SHOP", "스퀘어": "SQ", "페이팔": "PYPL", "비자": "V",
    "버크셔": "BRK-B", "존슨앤존슨": "JNJ", "화이자": "PFE", "모더나": "MRNA",
    "오라클": "ORCL",
    # 지수/ETF
    "다우": "^DJI", "나스닥": "^IXIC", "S&P": "^GSPC", "sp500": "^GSPC",
    "QQQ": "QQQ", "SPY": "SPY",
    # 가상화폐
    "비트코인": "BTC-USD", "이더리움": "ETH-USD", "리플": "XRP-USD",
    "솔라나": "SOL-USD", "도지코인": "DOGE-USD",
}

# 역매핑: 티커 → 회사명
_SYMBOL_TO_NAME = {v: k for k, v in _OVERSEAS_MAP.items()}


def stock_price_overseas(query: str) -> str:
    """한국어 종목명 → 티커 매핑 → yfinance Search 폴백으로 해외 주가 조회"""
    try:
        q_lower = query.lower()
        symbol = None

        # 1) 한국어 매핑 우선
        for kor, ticker in _OVERSEAS_MAP.items():
            if kor.lower() in q_lower:
                symbol = ticker
                break

        # 2) 알파벳 티커 직접 포함 (PLTR, TSLA 등)
        if not symbol:
            m = re.search(r'\b([A-Z]{2,5})\b', query)
            if m:
                symbol = m.group(1)

        # 3) yfinance Search 폴백 (한국어 키워드 → 영문 검색)
        if not symbol:
            clean = re.sub(
                r'(주가|현재가|가격|시세|얼마야|얼마에요|알려줘|조회|\?|!)', '', query
            ).strip().rstrip('요은는이가')
            if clean and len(clean) >= 2:
                results = yf.Search(clean, max_results=3).quotes
                # NASDAQ/NYSE 우선
                for r in results:
                    if r.get("exchange") in ("NMS", "NYQ", "NGM"):
                        symbol = r["symbol"]
                        break
                if not symbol and results:
                    symbol = results[0]["symbol"]

        if not symbol:
            return None

        price = None

        # 신선한 세션으로 데이터 조회 (캐시 우회)
        try:
            session = requests.Session()
            session.headers.update({"User-Agent": "Mozilla/5.0"})
            hist = yf.download(symbol, period="5d", progress=False, auto_adjust=True, session=session)
            if not hist.empty:
                # 가장 최근 종가 (오늘 또는 마지막 거래일)
                price = float(hist["Close"].iloc[-1])
        except Exception as e:
            logger.debug("download 실패 (%s): %s", symbol, str(e))
            pass

        # 폴백: history 직접 호출
        if not price:
            try:
                t = yf.Ticker(symbol)
                hist = t.history(period="1d")
                if not hist.empty:
                    price = float(hist["Close"].iloc[-1])
            except Exception as e:
                logger.debug("history 실패 (%s): %s", symbol, str(e))
                pass

        # 최후 폴백: fast_info (실시간성 낮음, 주의)
        if not price:
            try:
                t = yf.Ticker(symbol)
                price = t.fast_info.last_price
            except Exception as e:
                logger.debug("fast_info 실패 (%s): %s", symbol, str(e))
                pass

        if price:
            currency = "₩" if symbol.endswith(".KS") else "$"
            # 회사명 찾기
            company_name = _SYMBOL_TO_NAME.get(symbol, symbol)
            return f"{company_name} {symbol} 현재가: {currency}{price:,.2f}"
        return None
    except Exception:
        logger.exception("stock_price_overseas 예외: %s", query)
        return None


# -------------------------
# 네이버 Finance에서 외국인/기관 순매수 상위 종목 스크래핑
def _naver_net_buy_list(investor_gubun='9000', sosok='01', buy_type='buy', date_str=None):
    """네이버 Finance 순매수 상위 종목 반환.
    investor_gubun: 9000=외국인, 1000=금융투자
    date_str: 'YYYYMMDD' 형식 과거 날짜 (None=오늘)
    """
    try:
        from io import StringIO
        url = (f"https://finance.naver.com/sise/sise_deal_rank_iframe.naver"
               f"?sosok={sosok}&investor_gubun={investor_gubun}&type={buy_type}")
        if date_str:
            url += f"&ntp={date_str}"
        headers = {
            'User-Agent': 'Mozilla/5.0',
            'Referer': 'https://finance.naver.com/sise/sise_deal_rank.naver'
        }
        r = requests.get(url, headers=headers, timeout=10)
        dfs = pd.read_html(StringIO(r.text))
        for df in dfs:
            if '종목명' in df.columns:
                df = df.dropna(subset=['종목명']).reset_index(drop=True)
                return df
    except Exception:
        logger.exception("_naver_net_buy_list 예외")
    return None


def get_foreign_net_buy(query: str) -> str:
    try:
        kst = pytz.timezone('Asia/Seoul')
        today = datetime.datetime.now(kst)
        date_str = today.strftime("%Y%m%d")

        df_foreign = _naver_net_buy_list('9000', '01', 'buy')
        if df_foreign is None or df_foreign.empty:
            return "현재 외국인 순매수 데이터를 불러올 수 없습니다. (네이버 Finance 조회 실패)"

        df_inst = _naver_net_buy_list('1000', '01', 'buy')
        inst_names = set(df_inst['종목명'].tolist()) if df_inst is not None and not df_inst.empty else set()

        result_text = f"🔥 [{date_str}] 외국인 순매수 상위 {len(df_foreign)}선 🔥\n"
        result_text += "(⭐ = 기관도 동시 순매수)\n\n"
        for i, row in df_foreign.iterrows():
            name = row['종목명']
            amount = row.get('금액', 0)
            overlap = "⭐" if name in inst_names else "  "
            amt_str = f"({int(amount):,}백만원)" if pd.notna(amount) else ""
            result_text += f"{overlap}{i+1}위. {name} {amt_str}\n"

        both = [r['종목명'] for _, r in df_foreign.iterrows() if r['종목명'] in inst_names]
        if both:
            result_text += f"\n🎯 외국인+기관 동반매수: {', '.join(both)}"

        return result_text
    except Exception:
        logger.exception("get_foreign_net_buy 예외")
        return "데이터 조회 실패"


def _get_today_institutional_net_buy() -> str:
    """Oracle DB에서 오늘 기관합계 순매수 TOP20 조회"""
    try:
        kst = pytz.timezone('Asia/Seoul')
        date_str = datetime.datetime.now(kst).strftime('%Y%m%d')
        p = get_db_pool()
        if not p:
            return None
        with p.acquire() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT name, net_buy_amount FROM mock_smart_flows "
                    "WHERE date_str = :1 AND investor_type = '기관합계' "
                    "ORDER BY rank_no ASC FETCH FIRST 20 ROWS ONLY",
                    [date_str]
                )
                rows = cur.fetchall()
        if not rows:
            return None
        lines = [f"🔥 [{date_str}] 기관 순매수 상위 {len(rows)}선 🔥\n"]
        for i, (name, amount) in enumerate(rows, 1):
            lines.append(f"{i}위. {name} ({int(amount) // 1_000_000:,}백만원)")
        return "\n".join(lines)
    except Exception:
        logger.exception("_get_today_institutional_net_buy 오류")
        return None


# -------------------------
# 네이버 뉴스 검색 도구
def naver_news(query: str) -> str:
    import html
    headers = {"X-Naver-Client-Id": config.NAVER_ID.strip(), "X-Naver-Client-Secret": config.NAVER_SECRET.strip()}
    api_url = "https://openapi.naver.com/v1/search/news.json"
    params = {"query": query.replace('"', '').replace("'", "").strip(), "display": 3, "sort": "sim"}
    try:
        r = requests.get(api_url, headers=headers, params=params, timeout=6)
        r.raise_for_status()
        titles = [html.unescape(it.get("title", "").replace("<b>", "").replace("</b>", "")) for it in r.json().get("items", [])]
        return " / ".join(titles) if titles else "관련 뉴스가 없습니다."
    except Exception:
        logger.exception("naver_news 예외")
        return "네이버 연결 실패"


# -------------------------
# korea_invest_stock 통합
popular_stocks = {
    '삼성전자':'005930','lg엔솔':'373220','sk하이닉스':'000660','카카오뱅크':'323410',
    '삼성에피스':'010060','naver':'035420','카카오':'035720'
}


def korea_invest_stock(query: str) -> str:
    q = query.strip()
    # 입력 정제 — 불필요한 한글/기호 제거 (긴 단어부터 제거)
    q = re.sub(r'(조회해줘|조회해|알려줘|찾아줘|보여줘|주가|조회|얼마|시세|가격|현재가|보여|줘|알려|찾아|해)', ' ', q)
    q = " ".join(q.split()).strip()  # 연속 공백 제거
    logger.info("korea_invest_stock 호출 (정제후): %s", q)

    # 1) 6자리 코드 직접 입력 — 네이버(실시간) → KIS → yfinance
    if len(q) == 6 and q.isdigit():
        price = get_naver_price(q) or get_price_by_code(q) or get_yahoo_price(q)
        if price:
            save_stock_code_to_db("코드직입력", q)
            return f"{q} 현재가: {price}"
        return f"❌ `{q}`: 가격 조회 실패"

    # 2) DB 우선 (네이버 → KIS → yfinance 순서)
    code = get_stock_code_from_db(q)
    if code:
        price = get_naver_price(code) or get_price_by_code(code) or get_yahoo_price(code)
        if price:
            return f"{q} {code} 현재가: {price}"
        for attempt in range(2):
            time.sleep(0.3)
            price = get_naver_price(code) or get_price_by_code(code) or get_yahoo_price(code)
            if price:
                return f"{q} {code} 현재가: {price}"
        return f"🚨 '{q}' 코드({code}) 가격 조회 실패. 새 종목코드(6자리)를 알려주세요!"

    # 3) 인기종목 폴백 (네이버 → KIS → yfinance)
    for name, code in popular_stocks.items():
        if name.lower() in q.lower():
            price = get_naver_price(code) or get_price_by_code(code) or get_yahoo_price(code)
            if price:
                save_stock_code_to_db(name, code)
                return f"{name} {code} 현재가: {price}"
    # 4) 네이버 자동 학습 (네이버 → KIS → yfinance)
    code = naver_search_code(q)
    if code:
        save_stock_code_to_db(q, code)
        price = get_naver_price(code) or get_price_by_code(code) or get_yahoo_price(code)
        if price:
            return f"{q} {code} 현재가: {price}"
        return f"✅ {q} 코드 학습완료: `{code}` (가격 조회 실패)"
    return None  # 종목 미인식 — ask_ai가 LLM 직접 호출하도록 None 반환
