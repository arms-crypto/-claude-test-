#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
통합 실행 파일 (수정본): mistral-small:24b 기반
- LangChain, ChatGoogleGenerativeAI, ChatGroq 완전 제거.
- Gemini / Groq는 보조 도구로만 사용 (코드 내는 LLM 로직 비중 최소화).
- 외부 API 호출, 버전/의존성 충돌을 최소화.
- mistral-small:24b를 HTTP REST로 호출하여 챗봇/트레이딩 규칙 생성.
"""

import os
import json
import re
import time
import threading
import datetime
import logging
import requests
from bs4 import BeautifulSoup
import yfinance as yf
import pandas as pd
from flask import Flask, request, jsonify
from flask_cors import CORS
import oracledb
import pytz
from mock_trading.telegram_handler import parse_mock_command

# -------------------------
# 앱 생성
app = Flask(__name__)
CORS(app)

# -------------------------
# 로깅 설정
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("proxy_v53")

# -------------------------
# 민감정보 (요청대로 원본 포함)
TOKEN_RAW = "8707168013:AAH5yIsoaLoxcA0Lthiw7RaIzD1YcJx8cc8"
CHAT_ID = "8448138406"
NAVER_ID = "6MSVizApP3DYXeUhor5J"
NAVER_SECRET = "WCddJHD62B"
APP_KEY = "PSY9gMy15uipajb9qM25Cj1Uhf74FVu1cDyF"
APP_SECRET = "A/vwnErWUmOrZFUoJQ5bBS78WdY1lS6T6GaD5Hx1dNE+J3TTxTi1QwBvdFZuoKHWJ2nKEz+SaAmZmNikWH04Ge4Mm7up+/5JeAphHOXYld5nIbtehEmHMFcHVeB3EbNQem1pi2+0cVdyj6w7UzGJA+HqVRNFlPapifykRfPmf4Qf0IaIJdU="
DB_USER = "admin"
DB_PASS = "Flavor121212"
DB_DSN = "nzdrpgcmwjtme3py_high"
DB_WALLET_DIR = "/home/ubuntu/oracle_task/wallet_dbname"
DB_WALLET_PASS = "Flavor121212"
URL = "https://openapivts.koreainvestment.com:443"

# -------------------------
# 실시간 검색 설정 (SearXNG + Perplexica)
SEARXNG_URL = "http://localhost:8080"       # Docker 포트 매핑
PERPLEXICA_URL = "http://localhost:3001"    # Perplexica Backend API

# -------------------------
# 전역 캐시 및 스토어
hantu_token_cache = {"token": None, "expires_at": 0}
store = {}  # session_id -> ChatMessageHistory
verified_facts_store = {}  # session_id -> list of verified facts
pool = None  # 오라클 DB 풀

# -------------------------
# DB 풀 생성 함수
def get_db_pool():
    global pool
    if pool is None:
        try:
            logger.info("오라클 DB 연결 시도...")
            pool = oracledb.create_pool(
                user=DB_USER,
                password=DB_PASS,
                dsn=DB_DSN,
                min=1,
                max=2,
                config_dir=DB_WALLET_DIR,
                wallet_location=DB_WALLET_DIR,
                wallet_password=DB_WALLET_PASS
            )
            logger.info("오라클 DB 풀 생성 성공")
        except Exception:
            logger.exception("DB 풀 생성 실패")
            pool = None
    return pool

# -------------------------
# DB 저장/조회 유틸
def save_fact_to_db(content: str):
    p = get_db_pool()
    if not p:
        logger.warning("DB 풀 없음: save_fact_to_db 스킵")
        return
    try:
        with p.acquire() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO daily_news (headlines, run_time) VALUES (:1, CURRENT_TIMESTAMP)",
                    [str(content)[:1000]]
                )
                conn.commit()
                logger.info("DB에 팩트 저장 완료")
    except Exception:
        logger.exception("DB 저장 오류")

def get_stock_code_from_db(name: str) -> str:
    p = get_db_pool()
    if not p:
        return None
    try:
        with p.acquire() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT code FROM stock_codes 
                    WHERE UPPER(TRIM(name)) LIKE UPPER(:1)
                    ORDER BY LENGTH(name)
                    FETCH FIRST 1 ROWS ONLY
                """, [f"%{name.strip()}%"])
                res = cur.fetchone()
                return res[0] if res else None
    except Exception:
        logger.exception("get_stock_code_from_db 오류")
        return None

def save_stock_code_to_db(name: str, code: str) -> bool:
    p = get_db_pool()
    if not p:
        logger.warning("DB 풀 없음: save_stock_code_to_db 스킵")
        return False
    try:
        with p.acquire() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT code FROM stock_codes WHERE name = :1", [name])
                if cur.fetchone():
                    cur.execute("UPDATE stock_codes SET code = :2, updated = CURRENT_TIMESTAMP WHERE name = :1", [name, code])
                else:
                    cur.execute("INSERT INTO stock_codes (name, code) VALUES (:1, :2)", [name, code])
                conn.commit()
                logger.info("종목코드 DB 저장: %s=%s", name, code)
                return True
    except Exception:
        logger.exception("save_stock_code_to_db 오류")
        return False

# -------------------------
# KRX 전체 종목 DB 초기화 (수동/한번 실행용)
def init_krx_db():
    logger.info("KRX 종목 DB 생성 시작...")
    try:
        url = "http://kind.krx.co.kr/corpgeneral/corpList.do?method=download"
        params = {'marketType': 'stockMkt', 'searchCodeType': '', 'pageIndex': '1'}
        r = requests.post(url, params=params, timeout=15)
        r.raise_for_status()
        df = pd.read_html(r.text)[0]
        df['회사명'] = df['회사명'].str.strip().str.replace(' ', '')
        df['종목코드'] = df['종목코드'].astype(str).str.zfill(6)
        p = get_db_pool()
        if p:
            with p.acquire() as conn:
                with conn.cursor() as cur:
                    try:
                        cur.execute("DELETE FROM stock_codes")
                    except Exception:
                        pass
                    for _, row in df.iterrows():
                        cur.execute("INSERT INTO stock_codes (name, code) VALUES (:1, :2)", [row['회사명'], row['종목코드']])
                    conn.commit()
            logger.info("KRX 종목 DB 생성 완료: %d 종목", len(df))
        return dict(zip(df['종목코드'], df['회사명']))
    except Exception:
        logger.exception("init_krx_db 실패")
        return {}

# -------------------------
# 토큰 발급 유틸 (통일된 이름)
def get_hantu_token():
    current_time = time.time()
    if hantu_token_cache["token"] and current_time < hantu_token_cache["expires_at"]:
        return hantu_token_cache["token"]
    try:
        auth_url = f"{URL}/oauth2/tokenP"
        body = {"grant_type": "client_credentials", "appkey": APP_KEY, "appsecret": APP_SECRET}
        r = requests.post(auth_url, json=body, timeout=6)
        r.raise_for_status()
        data = r.json()
        token = data.get("access_token")
        expires_in = int(data.get("expires_in", 12 * 60 * 60))
        if token:
            hantu_token_cache["token"] = token
            hantu_token_cache["expires_at"] = current_time + expires_in - 60
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
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "FHKST01010400"
    }
    params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code}
    try:
        r = requests.get(f"{URL}/uapi/domestic-stock/v1/quotations/inquire-price", params=params, headers=headers, timeout=8)
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
    """네이버 검색으로 종목코드 추출"""
    try:
        search_url = f"https://finance.naver.com/sise/sise_search.naver?query={query}"
        r = requests.get(search_url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=8)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, 'html.parser')
        link = soup.select_one('a[href*="code="]')
        if link and 'code=' in link['href']:
            code = link['href'].split('code=')[1].split('&')[0]
            return code
    except Exception:
        logger.exception("naver_search_code 예외")
    return None

# -------------------------
# 외국/해외 주가 조회 도구
def stock_price_overseas(query: str) -> str:
    try:
        stocks = {"엔비디아": "NVDA", "테슬라": "TSLA", "애플": "AAPL", "비트코인": "BTC-USD"}
        symbol = "NVDA"
        for k, v in stocks.items():
            if k in query:
                symbol = v
                break
        hist = yf.download(tickers=symbol, period="1d", progress=False)
        if not hist.empty:
            price = hist['Close'].iloc[-1].item()
            return f"🌍 **{symbol}** 현재가: **${price:.2f}**"
        return f"❌ {symbol}: 데이터를 찾을 수 없습니다."
    except Exception:
        logger.exception("stock_price_overseas 예외")
        return "❌ 해외 주가 오류"

# -------------------------
# 외국인 순매수 상위 20 및 차트 생성
def get_foreign_net_buy(query: str) -> str:
    try:
        from pykrx import stock
        import mplfinance as mpf
        kst = pytz.timezone('Asia/Seoul')
        today = datetime.datetime.now(kst)
        if today.weekday() == 5: today -= datetime.timedelta(days=1)
        elif today.weekday() == 6: today -= datetime.timedelta(days=2)
        date_str = today.strftime("%Y%m%d")
        df_net_buy = stock.get_market_net_purchases_of_equities_by_ticker(date_str, date_str, "KOSPI", "외국인")
        days_back = 1
        while df_net_buy.empty and days_back <= 10:
            check_date = today - datetime.timedelta(days=days_back)
            date_str = check_date.strftime("%Y%m%d")
            df_net_buy = stock.get_market_net_purchases_of_equities_by_ticker(date_str, date_str, "KOSPI", "외국인")
            days_back += 1
        if df_net_buy.empty:
            return "현재 한국거래소(KRX) 서버 점검으로 인해 순매수 데이터를 불러올 수 없습니다."
        top20 = df_net_buy.sort_values(by="순매수거래대금", ascending=False).head(20)
        result_text = f"🔥 [{date_str}] 외국인 순매수 상위 20선 🔥\\n\\n"
        for i, ticker in enumerate(top20.index):
            name = top20.loc[ticker, "종목명"]
            money_억원 = top20.loc[ticker, "순매수거래대금"] // 100000000
            result_text += f"{i+1}위. {name} ({money_억원:,}억)\\n"
        top1_ticker = top20.index[0]
        top1_name = top20.loc[top1_ticker, "종목명"]
        start_date = (datetime.datetime.now(kst) - datetime.timedelta(days=90)).strftime("%Y%m%d")
        ohlcv = stock.get_market_ohlcv(start_date, date_str, top1_ticker)
        if not ohlcv.empty:
            ohlcv.index.name = 'Date'
            ohlcv.rename(columns={'시가':'Open', '고가':'High', '저가':'Low', '종가':'Close', '거래량':'Volume'}, inplace=True)
            chart_filename = f"chart_{top1_ticker}.png"
            mpf.plot(ohlcv, type='candle', mav=(5, 20), volume=True, savefig=chart_filename, style='yahoo', title=f"{top1_name} (No.1 Foreign Net Buy)")
            return f"{result_text}\\n💡 외국인 순매수 1위 종목 '{top1_name}'의 최근 3개월 차트를 첨부합니다.\\n[IMAGE_PATH:{chart_filename}]"
        return f"{result_text}\\n(1위 종목 차트 데이터는 부족하여 텍스트만 전송합니다.)"
    except Exception:
        logger.exception("get_foreign_net_buy 예외")
        return "데이터 조회 실패"

# -------------------------
# 네이버 뉴스 검색 도구
def naver_news(query: str) -> str:
    headers = {"X-Naver-Client-Id": NAVER_ID.strip(), "X-Naver-Client-Secret": NAVER_SECRET.strip()}
    api_url = "https://openapi.naver.com/v1/search/news.json"
    params = {"query": query.replace('"', '').replace("'", "").strip(), "display": 3, "sort": "sim"}
    try:
        r = requests.get(api_url, headers=headers, params=params, timeout=6)
        r.raise_for_status()
        import html
        titles = [html.unescape(it.get("title", "").replace("<b>", "").replace("</b>", "")) for it in r.json().get("items", [])]
        return " / ".join(titles) if titles else "관련 뉴스가 없습니다."
    except Exception:
        logger.exception("naver_news 예외")
        return "네이버 연결 실패"

# -------------------------
# korea_invest_stock 통합 (단일 함수)
popular_stocks = {
    '삼성전자':'005930','lg엔솔':'373220','sk하이닉스':'000660','카카오뱅크':'323410',
    '삼성에피스':'010060','naver':'035420','카카오':'035720'
}

def korea_invest_stock(query: str) -> str:
    q = query.strip()
    logger.info("korea_invest_stock 호출: %s", q)

    # 1) 6자리 코드 직접 입력
    if len(q) == 6 and q.isdigit():
        price = get_yahoo_price(q) or get_price_by_code(q) or get_naver_price(q)
        if price:
            save_stock_code_to_db("코드직입력", q)
            return f"✅ `{q}` 현재가: {price} (직입력)"
        return f"❌ `{q}`: 가격 조회 실패"

    # 2) DB 우선 (Yahoo 우선)
    code = get_stock_code_from_db(q)
    if code:
        price = get_yahoo_price(code) or get_price_by_code(code) or get_naver_price(code)
        if price:
            return f"**{q}: {price}** (DB)"
        for attempt in range(2):
            time.sleep(0.3)
            price = get_price_by_code(code) or get_yahoo_price(code) or get_naver_price(code)
            if price:
                return f"**{q}: {price}** (DB, retry)"
        return f"🚨 '{q}' 코드({code}) 가격 조회 실패. 새 종목코드(6자리)를 알려주세요!"

    # 3) 인기종목 폴백
    for name, code in popular_stocks.items():
        if name.lower() in q.lower():
            price = get_yahoo_price(code) or get_naver_price(code)
            if price:
                save_stock_code_to_db(name, code)
                return f"**{name}: {price}** (폴백)"
    # 4) 네이버 자동 학습
    code = naver_search_code(q)
    if code:
        save_stock_code_to_db(q, code)
        price = get_yahoo_price(code) or get_price_by_code(code) or get_naver_price(code)
        if price:
            return f"**{q}: {price}** (신학습)"
        return f"✅ {q} 코드 학습완료: `{code}` (가격 조회 실패)"
    return f"❌ '{q}' 모름. 종목코드(예:005930)를 알려주세요!"

# -------------------------
# mistral-small:24b 단독 사용 설정 (다른 모델 사용 불가)

REMOTE_OLLAMA_IP = "221.144.111.116"
QWEN_URL         = f"http://{REMOTE_OLLAMA_IP}:11434/api/chat"
QWEN_MODEL       = "mistral-small:24b"   # 유일하게 허용된 모델
MISTRAL_MAX_RETRY = 3


def _parse_ollama_response(r) -> str:
    """Ollama chat 응답에서 텍스트 추출 (JSON / NDJSON 모두 처리)."""
    raw_text = r.text.strip()
    try:
        data = r.json()
        if isinstance(data, dict):
            if "message" in data:
                return data["message"]["content"]
            if "response" in data:
                return data["response"]
        if isinstance(data, list):
            parts = []
            for item in data:
                if isinstance(item, dict):
                    if "message" in item:
                        parts.append(item["message"].get("content", ""))
                    elif "response" in item:
                        parts.append(item["response"])
            return "\n".join(parts).strip()
        return str(data)
    except (json.JSONDecodeError, ValueError):
        # NDJSON (여러 줄 JSON) 처리
        for line in reversed(raw_text.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    if "message" in obj:
                        return obj["message"].get("content", "")
                    if "response" in obj:
                        return obj["response"]
            except Exception:
                continue
        return raw_text


def call_mistral_only(prompt: str, system: str = "당신은 한국어로 답변하는 AI 전문가입니다.") -> str:
    """
    mistral-small:24b 단독 호출. 3회 재시도 후 최종 실패 시 안내 메시지 반환.
    다른 모델로의 폴백은 없음.
    """
    payload = {
        "model": QWEN_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": prompt},
        ],
        "options": {"temperature": 0.7, "num_predict": 2048},
        "stream": False,
    }
    last_exc = None
    for attempt in range(1, MISTRAL_MAX_RETRY + 1):
        try:
            r = requests.post(QWEN_URL, json=payload, timeout=60)
            r.raise_for_status()
            result = _parse_ollama_response(r)
            if result:
                return result
        except Exception as e:
            last_exc = e
            wait = 2 ** (attempt - 1)   # 1s → 2s → 4s
            logger.warning("mistral-small:24b 시도 %d/%d 실패 (%s) — %ds 후 재시도",
                           attempt, MISTRAL_MAX_RETRY, str(e)[:80], wait)
            if attempt < MISTRAL_MAX_RETRY:
                time.sleep(wait)

    logger.error("mistral-small:24b %d회 모두 실패: %s", MISTRAL_MAX_RETRY, str(last_exc)[:200])
    return "⚠️ mistral 서버 불안정. 잠시 후 다시 시도해주세요.\n모의투자(/mock)는 정상 작동 중입니다."


# 기존 call_qwen 호출부 호환성 유지
call_qwen = call_mistral_only
# -------------------------
# Chat history helpers
def get_session_history(session_id):
    if session_id not in store:
        store[session_id] = []  # 문자열 리스트로 간단 채팅용, 실전에선 ChatMessageHistory도 가능
    return store[session_id]

def get_verified_facts(session_id):
    if session_id not in verified_facts_store:
        verified_facts_store[session_id] = []
    return "\n".join(verified_facts_store[session_id])

def get_fact_info_for_session(session_id: str) -> str:
    if session_id in verified_facts_store:
        return "\n".join(verified_facts_store[session_id])
    return ""

# -------------------------
# 팩트 검증 백그라운드
def check_and_store_fact(session_id, user_input, ai_response):
    check_prompt = f"""
    당신은 엄격한 팩트 체커입니다.
    다음 사용자의 말에서, 사용자의 '이름', '나이', '자산', '직업' 등 명확한 개인 정보나 영구적으로 기억해야 할 중요한 사실이 있다면 1문장으로 요약하세요.
    단, 농담, 인사말, 불확실한 미래 예측, 단순 질문이라면 무조건 'None'이라고만 대답하세요.

    사용자: {user_input}
    AI: {ai_response}
    """
    try:
        fact_result = call_qwen(check_prompt)
        if "None" not in fact_result and len(fact_result.strip()) > 5:
            if session_id not in verified_facts_store:
                verified_facts_store[session_id] = []
            verified_facts_store[session_id].append(fact_result.strip())
            logger.info("팩트 검증 성공, 저장: %s", fact_result.strip())
    except Exception:
        logger.exception("check_and_store_fact 예외")

# -------------------------
# get_latest_db_news
def get_latest_db_news(query: str = "") -> str:
    p = get_db_pool()
    if not p:
        return "DB 연결 상태가 아닙니다."
    try:
        with p.acquire() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT headlines FROM daily_news ORDER BY run_time DESC FETCH FIRST 1 ROWS ONLY")
                res = cur.fetchone()
                return f"DB 최신 뉴스: {res[0]}" if res else "저장된 뉴스가 없습니다."
    except Exception:
        logger.exception("get_latest_db_news 예외")
        return "DB 조회 오류"

# -------------------------
# ask_ai 핵심 로직 (mistral-small:24b 기반)
def ask_ai(session_id, user_input):
    history = get_session_history(session_id)

    # 1) 6자리 코드만 입력시 자동학습 (그대로 유지)
    if len(user_input.strip()) == 6 and user_input.strip().isdigit():
        code = user_input.strip()
        save_stock_code_to_db("코드학습", code)
        price = get_price_by_code(code) or get_yahoo_price(code) or get_naver_price(code)
        if price:
            return f"✅ `{code}` 자동학습! 💾\\n현재가: **{price}**", None
        return f"❌ `{code}`: 가격 조회 실패", None

    # 2) 이름만 입력시 DB 조회 (그대로 유지)
    clean_name = re.sub(r'\\d{6}', '', user_input).strip()
    if clean_name and len(clean_name) > 1:
        code = get_stock_code_from_db(clean_name)
        if code:
            price = get_price_by_code(code) or get_yahoo_price(code) or get_naver_price(code)
            if price:
                return f"**{clean_name}**: {price} (DB) 💾\\n`[{code}]`", None

    # 3) 시간/팩트/채팅 기록 준비 (그대로 유지)
    now = datetime.datetime.now(pytz.timezone('Asia/Seoul'))
    current_time_str = now.strftime("%Y년 %m월 %d일 %p %I:%M")
    my_facts = get_verified_facts(session_id)
    fact_str = f"[사용자 핵심 정보]\\n{my_facts}\\n\\n" if my_facts else ""
    past_messages = history[-10:] if len(history) > 0 else []
    chat_history_str = "\\n".join(past_messages)

    # 4) 도구(주가/뉴스/순매수) 실행 로직
    tool_info = []  # 여기에 실제 결과를 모음

    # a) 주가/가격/얼마 관련 키워드면 도구 호출
    if any(k in user_input.lower() for k in ["주가", "가격", "얼마", "증권", "시세", "开盘", "종가", "시가"]):
        overseas = stock_price_overseas(user_input)
        korea = korea_invest_stock(user_input)
        if overseas:
            tool_info.append("✈️ 해외 주가: " + overseas)
        if korea:
            tool_info.append("🇰🇷 국내 주가: " + korea)

    # b) 뉴스/검색/related 정보 요청이면
    search_triggered = any(k in user_input.lower() for k in ["뉴스", "검색", "관련", "최신", "오늘", "동향", "전망", "분석"])
    if search_triggered:
        news = naver_news(user_input)
        if news:
            tool_info.append("📰 네이버 뉴스: " + news)
        # SearXNG 웹 검색 보강
        web_result = search_and_summarize(user_input)
        if web_result and web_result != "검색 결과가 없습니다.":
            tool_info.append("🌐 웹 검색 요약: " + web_result)
        # 검색 트리거됐는데 결과 없으면 명시적 안내 추가
        if not tool_info:
            tool_info.append("⚠️ 실시간 검색 결과 없음: 검색 엔진(SearXNG)에 연결할 수 없거나 결과가 없습니다. 최신 정보를 제공할 수 없습니다.")

    # c) 외국인/기관/순매수 요청이면
    if "순매수" in user_input.lower() or "순매매" in user_input.lower():
        fnb = get_foreign_net_buy(user_input)
        if fnb:
            tool_info.append("📈 순매수/매매 동향: " + fnb)

    # 5) LLM 호출 (이 부분이 핵심)
    try:
        if tool_info:
            # 검색 모드: 도구가 가져온 정보 + LLM 정리
            tool_str = "\n".join(tool_info)
            prompt = f"""현재 시각: {current_time_str}

사용자 핵심 정보:
{my_facts}

이전 대화:
{chat_history_str}

[수집된 도구 정보]
{tool_str}

사용자 질문: {user_input}

위 도구 정보를 활용하여 한국어로 답변. 단, 도구 정보에 관련 내용이 없으면 "실시간 검색 결과에 해당 정보가 없습니다. 검색 엔진을 확인해주세요." 라고 안내:"""
            answer = call_qwen(prompt)
        else:
            # 일반 대화 모드
            prompt = f"""
현재 시각: {current_time_str}

사용자 핵심 정보:
{my_facts}

이전 대화:
{chat_history_str}

질문: {user_input}

응답: 위 질문에 대해 자연스럽게 한국어로 답변하라.
            """
            answer = call_qwen(prompt)

        # 6) 이미지 처리 등은 그대로 유지
        image_path = None
        if "[IMAGE_PATH:" in answer:
            start = answer.find("[IMAGE_PATH:") + 12
            end = answer.find("]", start)
            if start >= 12 and end > start:
                image_path = answer[start:end]
                answer = answer[:start].strip()

    except Exception:
        logger.exception("ask_ai 내부 예외")
        answer = "시스템 오류가 발생했습니다. 잠시 후 다시 시도해주세요."

    # 7) 채팅 기록 및 팩트 저장 (이 아래도 그대로 유지)
    history.append(f"Human: {user_input}")
    history.append(f"AI: {answer}")

    threading.Thread(target=check_and_store_fact, args=(session_id, user_input, answer)).start()

    if any(k in answer for k in ["원", "$", "매수"]):
        threading.Thread(target=save_fact_to_db, args=(answer,)).start()

    return answer, image_path

# -------------------------
# 자동 스케줄러 및 텔레그램 감시
def auto_report_scheduler():
    kst = pytz.timezone('Asia/Seoul')
    base_url = f"https://api.telegram.org/bot{TOKEN_RAW}"
    last_run_time = None
    logger.info("자동 보고서 스케줄러 시작")
    while True:
        try:
            now = datetime.datetime.now(kst)
            if now.weekday() < 5:
                if now.hour == 8 and now.minute == 40 and last_run_time != "morning":
                    prompt = "오늘 장 시작 전 확인할만한 최신 경제 뉴스를 검색해서 요약해줘."
                    reply_text, _ = ask_ai("auto_scheduler", prompt)
                    requests.post(f"{base_url}/sendMessage", json={"chat_id": CHAT_ID, "text": f"🌅 [장 시작 전 AI 프리뷰]\\n\\n{reply_text}"})
                    last_run_time = "morning"
                elif now.hour == 16 and now.minute == 0 and last_run_time != "afternoon":
                    prompt = "오늘 외국인 순매수 1위 종목을 차트와 함께 확인하고, 최신 증시 뉴스를 요약해줘."
                    reply_text, image_path = ask_ai("auto_scheduler", prompt)
                    requests.post(f"{base_url}/sendMessage", json={"chat_id": CHAT_ID, "text": f"📊 [장 마감 AI 요약 보고서]\\n\\n{reply_text}"})
                    if image_path and os.path.exists(image_path):
                        with open(image_path, "rb") as photo:
                            requests.post(f"{base_url}/sendPhoto", data={"chat_id": CHAT_ID}, files={"photo": photo})
                        os.remove(image_path)
                    last_run_time = "afternoon"
            if now.hour == 0 and now.minute == 0:
                last_run_time = None
        except Exception:
            logger.exception("auto_report_scheduler 예외")
        time.sleep(30)


def handle_tg():
    last_id = 0
    base_url = f"https://api.telegram.org/bot{TOKEN_RAW}"
    logger.info("텔레그램 감시 엔진 시작")
    while True:
        try:
            res = requests.get(f"{base_url}/getUpdates", params={"offset": last_id+1, "timeout": 10}, timeout=15).json()
            if res.get("ok") and res.get("result"):
                for up in res["result"]:
                    last_id = up["update_id"]
                    msg = up.get("message", {})
                    if str(msg.get("chat", {}).get("id", "")) == CHAT_ID and "text" in msg:
                        msg_text = msg["text"]
                        # /mock 명령어는 모의투자 핸들러로 라우팅
                        if msg_text.strip().startswith("/mock"):
                            reply_text = parse_mock_command(msg_text, oracle_pool=get_db_pool())
                            requests.post(f"{base_url}/sendMessage", json={"chat_id": CHAT_ID, "text": reply_text})
                            continue
                        reply_text, image_path = ask_ai(CHAT_ID, msg_text)
                        requests.post(f"{base_url}/sendMessage", json={"chat_id": CHAT_ID, "text": reply_text})
                        if image_path and os.path.exists(image_path):
                            with open(image_path, "rb") as photo:
                                requests.post(f"{base_url}/sendPhoto", data={"chat_id": CHAT_ID}, files={"photo": photo})
                            os.remove(image_path)
        except Exception:
            logger.exception("handle_tg 루프 예외")
            time.sleep(5)

# -------------------------
# DB 초기화: ensure_db_initialized 정의
def ensure_db_initialized():
    """
    앱 시작 시 한 번만 안전하게 DB 초기화를 수행합니다.
    """
    try:
        init_stock_codes_db()
    except Exception:
        logger.exception("ensure_db_initialized 예외")


def init_stock_codes_db():
    p = get_db_pool()
    if p:
        try:
            with p.acquire() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        CREATE TABLE stock_codes (
                            name VARCHAR2(50) PRIMARY KEY,
                            code VARCHAR2(10),
                            updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            fail_count NUMBER DEFAULT 0
                        )
                    """)
                    conn.commit()
                    logger.info("stock_codes 테이블 생성")
                    cur.execute("""
                        MERGE INTO stock_codes t
                        USING (SELECT 'LG엔솔' name, '373220' code FROM dual) s
                        ON (t.name = s.name)
                        WHEN MATCHED THEN UPDATE SET code = s.code
                        WHEN NOT MATCHED THEN INSERT (name, code) VALUES (s.name, s.code)
                    """)
                    conn.commit()
                    logger.info("LG엔솔(373220) DB 등록 완료")
        except Exception as e:
            if "ORA-00955" in str(e):
                logger.warning("테이블 이미 존재")
            else:
                logger.exception("init_stock_codes_db 예외")
        # mock_trades 테이블 (모의투자 거래내역 Oracle 백업)
        try:
            with p.acquire() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        CREATE TABLE mock_trades (
                            id         NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                            ticker     VARCHAR2(10),
                            name       VARCHAR2(50),
                            action     VARCHAR2(4),
                            price      NUMBER,
                            qty        NUMBER,
                            amount     NUMBER,
                            cash_after NUMBER,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        )
                    """)
                    conn.commit()
                    logger.info("mock_trades 테이블 생성")
        except Exception as e:
            if "ORA-00955" in str(e):
                logger.info("mock_trades 테이블 이미 존재")
            else:
                logger.exception("mock_trades 테이블 생성 실패 (무시)")

# -------------------------
# 실시간 검색 유틸 함수

def searxng_search(query: str, categories: str = "general", max_results: int = 5) -> list:
    """SearXNG에서 실시간 검색 결과를 가져온다."""
    try:
        r = requests.get(
            f"{SEARXNG_URL}/search",
            params={"q": query, "format": "json", "categories": categories, "language": "ko-KR"},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        results = data.get("results", [])[:max_results]
        return [
            {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "content": item.get("content", ""),
            }
            for item in results
        ]
    except Exception:
        logger.exception("SearXNG 검색 실패: %s", query)
        return []


def perplexica_search(query: str, focus_mode: str = "webSearch") -> str:
    """Perplexica Backend API를 통해 AI 검색 응답을 받는다.
    focus_mode: webSearch | academicSearch | writingAssistant | wolframAlphaSearch | youtubeSearch | redditSearch
    """
    try:
        payload = {
            "chatModel": {
                "provider": "ollama",
                "model": QWEN_MODEL,
                "customOpenAIBaseURL": f"http://{REMOTE_OLLAMA_IP}:11434",
                "customOpenAIKey": "",
            },
            "embeddingModel": {
                "provider": "ollama",
                "model": "nomic-embed-text:latest",
            },
            "optimizationMode": "speed",
            "focusMode": focus_mode,
            "query": query,
            "history": [],
        }
        r = requests.post(
            f"{PERPLEXICA_URL}/api/chat",
            json=payload,
            timeout=60,
        )
        r.raise_for_status()
        # Perplexica는 NDJSON 스트림으로 응답
        lines = r.text.strip().splitlines()
        answer_parts = []
        sources = []
        for line in lines:
            try:
                obj = json.loads(line)
                if obj.get("type") == "response":
                    answer_parts.append(obj.get("data", ""))
                elif obj.get("type") == "sources":
                    sources = obj.get("data", [])
            except Exception:
                continue
        answer = "".join(answer_parts)
        if sources:
            src_lines = "\n".join(
                f"- [{s.get('metadata', {}).get('title', s.get('pageContent','')[:40])}]({s.get('metadata', {}).get('url', '')})"
                for s in sources[:3]
            )
            answer += f"\n\n**출처:**\n{src_lines}"
        return answer if answer else "검색 결과를 찾지 못했습니다."
    except Exception:
        logger.exception("Perplexica 검색 실패: %s", query)
        return None


def search_and_summarize(query: str) -> str:
    """SearXNG로 검색 후 Ollama(mistral-small:24b)로 요약 - Perplexica 장애 시 폴백."""
    results = searxng_search(query, max_results=5)
    if not results:
        return "검색 결과가 없습니다."
    snippets = "\n\n".join(
        f"[{i+1}] {r['title']}\n{r['content']}\nURL: {r['url']}"
        for i, r in enumerate(results)
    )
    prompt = (
        f"다음은 '{query}'에 대한 실시간 웹 검색 결과입니다.\n\n"
        f"{snippets}\n\n"
        "위 검색 결과를 바탕으로 핵심 내용을 한국어로 간결하게 요약해 주세요. "
        "출처 URL도 함께 언급해 주세요."
    )
    return call_qwen(prompt)


# -------------------------
# Flask 엔드포인트
@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok"}), 200

@app.route('/ask', methods=['POST'])
def ask():
    msg = request.json.get("message", "")
    reply_text, _ = ask_ai("web_user", msg)
    return jsonify({"reply": reply_text})


@app.route('/mock', methods=['POST'])
def mock_trade():
    """
    모의투자 REST 엔드포인트.

    Request JSON:
        { "command": "/mock 현황" }
        { "command": "/mock 삼성전자 100만원 매수" }
        { "command": "/mock 매도 005930" }

    Response JSON:
        { "result": "..." }
    """
    data = request.json or {}
    command = data.get("command", "").strip()
    if not command:
        return jsonify({"error": "command 파라미터가 필요합니다."}), 400
    if not command.startswith("/mock"):
        command = "/mock " + command
    logger.info("/mock 요청: %s", command)
    result = parse_mock_command(command, oracle_pool=get_db_pool())
    return jsonify({"result": result})


@app.route('/search', methods=['POST'])
def search():
    """
    실시간 웹 검색 엔드포인트.

    Request JSON:
        {
            "query": "검색할 내용",
            "mode": "perplexica" | "searxng" | "auto"  (기본: "auto"),
            "focus": "webSearch" | "academicSearch" | ...  (Perplexica 전용, 기본: "webSearch")
        }

    Response JSON:
        {
            "query": "...",
            "answer": "...",
            "mode_used": "perplexica" | "searxng"
        }
    """
    data = request.json or {}
    query = data.get("query", "").strip()
    mode = data.get("mode", "auto")
    focus = data.get("focus", "webSearch")

    if not query:
        return jsonify({"error": "query 파라미터가 필요합니다."}), 400

    logger.info("/search 요청: query=%s mode=%s", query, mode)

    answer = None
    mode_used = mode

    # Perplexica 우선 시도 (mode=perplexica 또는 auto)
    if mode in ("perplexica", "auto"):
        answer = perplexica_search(query, focus_mode=focus)
        mode_used = "perplexica"

    # Perplexica 실패 시 또는 mode=searxng이면 SearXNG+Ollama 폴백
    if not answer or mode == "searxng":
        answer = search_and_summarize(query)
        mode_used = "searxng"

    return jsonify({"query": query, "answer": answer, "mode_used": mode_used})

# -------------------------
# 메인 실행부
if __name__ == "__main__":
    # 1) 앱 시작 시 DB 초기화를 백그라운드에서 한 번만 실행
    def _run_db_init_once():
        try:
            time.sleep(1)
            ensure_db_initialized()
        except Exception:
            logger.exception("백그라운드 DB 초기화 실패")

    threading.Thread(target=_run_db_init_once, daemon=True).start()

    # 2) 텔레그램 감시 스레드 실행
    threading.Thread(target=handle_tg, daemon=True).start()

    # 3) 자동 보고서 스케줄러 스레드 실행
    threading.Thread(target=auto_report_scheduler, daemon=True).start()

    # 4) Flask 웹 서버 실행
    app.run(host="0.0.0.0", port=11435)
