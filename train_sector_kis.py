#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
train_sector_kis.py — KIS 10년 데이터 기반 업종별 차트 기법 학습

흐름:
  1. 업종/테마별 대형·중형 종목 정의
  2. KIS API 연도별 청크로 10년치 월봉/주봉/일봉 수집
  3. 각 시점에서 12신호 계산 + 이후 수익률 레이블링
  4. Ollama에게 업종·타임프레임별 학습 질문
  5. chart_method_memory RAG에 저장
"""

import sys, os, time, hashlib, logging, json, sqlite3, datetime, requests, threading
sys.path.insert(0, "/home/ubuntu/-claude-test-")
os.chdir("/home/ubuntu/-claude-test-")

import pandas as pd
import numpy as np
import ta
from concurrent.futures import ThreadPoolExecutor, as_completed


# ── PC 절전 방지 킵얼라이브 ──────────────────────────────────────────────────
def _start_keepalive(interval_sec: int = 270):
    """
    5분(270초)마다 /ping_sleep_timer 호출 → _last_ollama_request 갱신.
    학습 스크립트가 llm_client를 거치지 않아 절전 타이머 미갱신 문제 해결.
    """
    def _loop():
        while True:
            time.sleep(interval_sec)
            try:
                r = requests.get("http://localhost:11435/ping_sleep_timer", timeout=5)
                logger.info("[킵얼라이브] 슬립 타이머 리셋 → %s", r.json())
            except Exception as e:
                logger.warning("[킵얼라이브] ping 실패: %s", e)
    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    logger.info("킵얼라이브 스레드 시작 (%.0f분 간격)", interval_sec / 60)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("train_sector_kis.log", encoding="utf-8"),
    ]
)
logger = logging.getLogger("train_sector")

# ── 설정 ─────────────────────────────────────────────────────────────────────
OLLAMA_PC    = "http://221.144.111.116:11434/api/chat"
OLLAMA_LOCAL = "http://localhost:11434/api/chat"
EMBED_URL    = "http://localhost:11434/api/embeddings"
EMBED_MODEL  = "nomic-embed-text"
CHROMA_DIR   = os.path.join(os.path.dirname(__file__), "rag_data")
DB_PATH      = os.path.join(os.path.dirname(__file__), "mock_trading", "sector_signal.db")

# ── 업종/테마별 종목 (대형·중형 2~3개) ────────────────────────────────────────
SECTOR_STOCKS = {
    # ── 코스피 대형·중형 ────────────────────────────────────────────
    "반도체": [
        ("005930", "삼성전자",       "대형"),
        ("000660", "SK하이닉스",     "대형"),
        ("042700", "한미반도체",     "중형"),
        ("336370", "솔브레인홀딩스", "중형"),
        ("240810", "원익IPS",        "중형"),
    ],
    "방산": [
        ("012450", "한화에어로스페이스", "대형"),
        ("079550", "LIG넥스원",      "중형"),
        ("064350", "현대로템",       "중형"),
        ("047050", "포스코인터내셔널","중형"),
    ],
    "자동차": [
        ("005380", "현대차",         "대형"),
        ("000270", "기아",           "대형"),
        ("012330", "현대모비스",     "대형"),
        ("011210", "현대위아",       "중형"),
        ("161390", "한국타이어앤테크놀로지","중형"),
    ],
    "2차전지": [
        ("373220", "LG에너지솔루션", "대형"),
        ("006400", "삼성SDI",        "대형"),
        ("247540", "에코프로비엠",   "중형"),
        ("086520", "에코프로",       "중형"),
        ("096530", "씨젠",           "중형"),
        ("278280", "천보",           "중형"),
    ],
    "바이오": [
        ("207940", "삼성바이오로직스","대형"),
        ("068270", "셀트리온",       "대형"),
        ("128940", "한미약품",       "중형"),
        ("196170", "알테오젠",       "중형"),
        ("028300", "HLB",            "중형"),
        ("141080", "리가켐바이오",   "중형"),
    ],
    "IT플랫폼": [
        ("035420", "NAVER",          "대형"),
        ("035720", "카카오",         "대형"),
        ("018260", "삼성SDS",        "중형"),
        ("259960", "크래프톤",       "대형"),
        ("036570", "엔씨소프트",     "대형"),
    ],
    "금융": [
        ("105560", "KB금융",         "대형"),
        ("055550", "신한지주",       "대형"),
        ("086790", "하나금융지주",   "대형"),
        ("316140", "우리금융지주",   "대형"),
        ("032830", "삼성생명",       "대형"),
        ("000810", "삼성화재",       "대형"),
    ],
    "철강/소재": [
        ("005490", "POSCO홀딩스",    "대형"),
        ("004020", "현대제철",       "중형"),
        ("010060", "OCI홀딩스",      "중형"),
        ("011000", "합성섬유",       "중형"),
        ("002380", "KCC",            "중형"),
    ],
    "건설": [
        ("000720", "현대건설",       "대형"),
        ("047040", "대우건설",       "중형"),
        ("006360", "GS건설",         "중형"),
        ("000080", "하이트진로",     "중형"),
        ("008770", "호텔신라",       "중형"),
    ],
    "에너지": [
        ("096770", "SK이노베이션",   "대형"),
        ("010950", "S-Oil",          "대형"),
        ("015760", "한국전력",       "대형"),
        ("267250", "HD현대",         "대형"),
    ],
    # ── 신규 업종 ────────────────────────────────────────────────────
    "조선/해운": [
        ("009540", "HD한국조선해양", "대형"),
        ("042660", "한화오션",       "대형"),
        ("010140", "삼성중공업",     "대형"),
        ("011200", "HMM",            "대형"),
        ("000120", "CJ대한통운",     "중형"),
    ],
    "반도체장비/소재": [
        ("403870", "HPSP",           "중형"),
        ("079940", "가비아",         "중형"),
        ("357780", "솔브레인",       "중형"),
        ("285130", "SK넥실리스",     "중형"),
        ("166090", "하나머티리얼즈", "중형"),
    ],
    "게임/엔터": [
        ("259960", "크래프톤",       "대형"),
        ("036570", "엔씨소프트",     "대형"),
        ("041510", "에스엠",         "중형"),
        ("035900", "JYP Ent.",       "중형"),
        ("352820", "하이브",         "대형"),
    ],
    "유통/소비": [
        ("139480", "이마트",         "대형"),
        ("004170", "신세계",         "대형"),
        ("069960", "현대백화점",     "대형"),
        ("007310", "오뚜기",         "중형"),
        ("271560", "오리온",         "중형"),
    ],
    "통신": [
        ("017670", "SK텔레콤",       "대형"),
        ("030200", "KT",             "대형"),
        ("032640", "LG유플러스",     "대형"),
    ],
    # ── ETF ──────────────────────────────────────────────────────────
    "ETF": [
        ("069500", "KODEX 200",              "ETF"),
        ("122630", "KODEX 레버리지",         "ETF"),
        ("229200", "KODEX 코스닥150",        "ETF"),
        ("396500", "TIGER 반도체TOP10",      "ETF"),
        ("494310", "KODEX 반도체레버리지",   "ETF"),
        ("462330", "TIGER 방산&우주",        "ETF"),
        ("267270", "KODEX 200선물인버스2X",  "ETF"),
    ],
}

# 날짜 청크 (5년씩 2회 = 10년)
DATE_CHUNKS = [
    ("20160101", "20201231"),
    ("20210101", "20261231"),
]


# ── KIS 데이터 수집 ───────────────────────────────────────────────────────────

def _kis_fetch_chunk(code: str, period: str, from_d: str, to_d: str) -> list:
    """KIS API 단일 날짜 범위 호출."""
    from mock_trading.kis_client import get_token, APP_KEY, APP_SECRET, KIS_URL
    token = get_token()
    if not token:
        return []
    headers = {
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "FHKST03010100",
    }
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": code,
        "FID_INPUT_DATE_1": from_d,
        "FID_INPUT_DATE_2": to_d,
        "FID_PERIOD_DIV_CODE": period,
        "FID_ORG_ADJ_PRC": "0",
    }
    try:
        from mock_trading.kis_client import KIS_URL as KU
        r = requests.get(
            f"{KU}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
            params=params, headers=headers, timeout=15,
            proxies={"http": None, "https": None},
        )
        r.raise_for_status()
        rows = []
        for item in r.json().get("output2", []):
            try:
                rows.append({
                    "date":   item.get("stck_bsop_date", ""),
                    "open":   int(item.get("stck_oprc", 0) or 0),
                    "high":   int(item.get("stck_hgpr", 0) or 0),
                    "low":    int(item.get("stck_lwpr", 0) or 0),
                    "close":  int(item.get("stck_clpr", 0) or 0),
                    "volume": int(item.get("acml_vol", 0) or 0),
                })
            except Exception:
                pass
        rows.reverse()  # 오래된 것부터
        return rows
    except Exception as e:
        logger.warning("KIS 조회 실패 %s %s %s~%s: %s", code, period, from_d, to_d, e)
        return []


def fetch_long_ohlcv(code: str, period: str) -> pd.DataFrame:
    """10년치 OHLCV 수집 — 5년씩 2청크 합산 후 정렬·중복 제거."""
    all_rows = []
    for from_d, to_d in DATE_CHUNKS:
        chunk = _kis_fetch_chunk(code, period, from_d, to_d)
        all_rows.extend(chunk)
        time.sleep(0.3)  # 레이트 리밋

    if not all_rows:
        return None
    df = pd.DataFrame(all_rows)
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["close", "high", "low"])
    df = df[df["close"] > 0]
    df = df.drop_duplicates("date").sort_values("date").reset_index(drop=True)
    return df


# ── 신호 계산 (auto_trader.py 동일) ──────────────────────────────────────────

def _ichimoku_signal(df: pd.DataFrame) -> bool:
    if df is None or len(df) < 3:
        return False
    c = df["close"]; h = df["high"]; l = df["low"]
    kijun = (h.rolling(1).max() + l.rolling(1).min()) / 2
    return float(c.iloc[-1]) >= float(kijun.iloc[-1]) * 0.99


def _four_signals(df: pd.DataFrame) -> dict:
    out = {k: False for k in ["ichimoku", "adx", "rsi", "macd"]}
    if df is None or len(df) < 6:
        return out
    c = df["close"]
    out["ichimoku"] = _ichimoku_signal(df)
    try:
        ind = ta.trend.ADXIndicator(df["high"], df["low"], c, window=3)
        adx = float(ind.adx().iloc[-1])
        out["adx"] = bool(adx > 7 and float(ind.adx_pos().iloc[-1]) > float(ind.adx_neg().iloc[-1]))
    except Exception:
        pass
    try:
        rsi = float(ta.momentum.rsi(c, window=6).iloc[-1]) if len(c) >= 7 else None
        out["rsi"] = bool(rsi and rsi > 50)
    except Exception:
        pass
    try:
        mh = float(ta.trend.macd_diff(c, window_fast=5, window_slow=13, window_sign=6).iloc[-1]) \
             if len(c) >= 14 else None
        out["macd"] = bool(mh and mh > 0)
    except Exception:
        pass
    return out


def calc_buy_count_from_dfs(df_m, df_w, df_d) -> tuple:
    """월봉/주봉/일봉 각각 4신호 → buy_count(0~12), 세부 신호 dict 반환."""
    s_m = _four_signals(df_m)
    s_w = _four_signals(df_w)
    s_d = _four_signals(df_d)
    count = sum([
        s_m["ichimoku"], s_m["adx"], s_m["rsi"], s_m["macd"],
        s_w["ichimoku"], s_w["adx"], s_w["rsi"], s_w["macd"],
        s_d["ichimoku"], s_d["adx"], s_d["rsi"], s_d["macd"],
    ])
    return count, {"월봉": s_m, "주봉": s_w, "일봉": s_d}


# ── DB 초기화 ─────────────────────────────────────────────────────────────────

def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS sector_signals (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            sector      TEXT,
            cap_type    TEXT,
            code        TEXT,
            name        TEXT,
            period_type TEXT,   -- M/W/D
            date        TEXT,
            buy_count   INTEGER,
            m_ich INTEGER, m_adx INTEGER, m_rsi INTEGER, m_macd INTEGER,
            w_ich INTEGER, w_adx INTEGER, w_rsi INTEGER, w_macd INTEGER,
            d_ich INTEGER, d_adx INTEGER, d_rsi INTEGER, d_macd INTEGER,
            next1_pct   REAL,   -- 1봉 후 수익률
            next3_pct   REAL,   -- 3봉 후 수익률
            next6_pct   REAL,   -- 6봉 후 수익률
            UNIQUE(code, period_type, date)
        )
    """)
    con.commit()
    con.close()
    logger.info("DB 초기화 완료: %s", DB_PATH)


def save_signals(rows: list):
    """신호 레코드 배치 저장."""
    if not rows:
        return
    con = sqlite3.connect(DB_PATH)
    con.executemany("""
        INSERT OR REPLACE INTO sector_signals
        (sector, cap_type, code, name, period_type, date, buy_count,
         m_ich, m_adx, m_rsi, m_macd,
         w_ich, w_adx, w_rsi, w_macd,
         d_ich, d_adx, d_rsi, d_macd,
         next1_pct, next3_pct, next6_pct)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, rows)
    con.commit()
    con.close()


# ── 종목별 신호 계산 + 레이블링 ───────────────────────────────────────────────

def process_stock(sector: str, code: str, name: str, cap_type: str) -> int:
    """
    KIS 10년 월봉/주봉/일봉 수집 → 각 시점 신호 계산 → DB 저장.
    반환: 저장된 레코드 수.
    """
    logger.info("수집 시작: %s(%s) [%s/%s]", name, code, sector, cap_type)

    df_m_full = fetch_long_ohlcv(code, "M")
    df_w_full = fetch_long_ohlcv(code, "W")
    df_d_full = fetch_long_ohlcv(code, "D")

    if df_m_full is None or len(df_m_full) < 10:
        logger.warning("월봉 데이터 부족: %s(%s) %s봉",
                       name, code, len(df_m_full) if df_m_full is not None else 0)
        return 0

    logger.info("  %s(%s): 월봉%d / 주봉%d / 일봉%d",
                name, code,
                len(df_m_full),
                len(df_w_full) if df_w_full is not None else 0,
                len(df_d_full) if df_d_full is not None else 0)

    records = []
    prices_m = df_m_full["close"].values
    dates_m  = df_m_full["date"].values
    n_m = len(df_m_full)

    # 월봉 기준으로 롤링 신호 계산 (최소 10봉부터)
    for i in range(10, n_m):
        date_str = dates_m[i]

        # 해당 시점까지의 슬라이스
        slice_m = df_m_full.iloc[:i+1]

        # 주봉: 해당 월봉 날짜까지만 필터
        if df_w_full is not None:
            slice_w = df_w_full[df_w_full["date"] <= date_str]
        else:
            slice_w = None

        # 일봉: 해당 월봉 날짜까지만 필터
        if df_d_full is not None:
            slice_d = df_d_full[df_d_full["date"] <= date_str]
            slice_d = slice_d.tail(120)  # 최근 120일봉만 사용
        else:
            slice_d = None

        buy_count, sig = calc_buy_count_from_dfs(slice_m, slice_w, slice_d)
        sm = sig["월봉"]; sw = sig["주봉"]; sd = sig["일봉"]

        # 이후 1/3/6봉 수익률 레이블링
        def ret(future_i):
            if future_i < n_m and prices_m[i] > 0:
                return round((float(prices_m[future_i]) - float(prices_m[i])) / float(prices_m[i]) * 100, 2)
            return None

        records.append((
            sector, cap_type, code, name, "M", date_str, buy_count,
            int(sm["ichimoku"]), int(sm["adx"]), int(sm["rsi"]), int(sm["macd"]),
            int(sw["ichimoku"]), int(sw["adx"]), int(sw["rsi"]), int(sw["macd"]),
            int(sd["ichimoku"]), int(sd["adx"]), int(sd["rsi"]), int(sd["macd"]),
            ret(i+1), ret(i+3), ret(i+6),
        ))

    save_signals(records)
    logger.info("  저장 완료: %s(%s) %d건", name, code, len(records))
    return len(records)


# ── Ollama 학습 ────────────────────────────────────────────────────────────────

def _ask_ollama(prompt: str) -> str:
    for url, model in [(OLLAMA_PC, "google_gemma-4-26b-a4b-it"), (OLLAMA_LOCAL, "gemma3:4b")]:
        try:
            r = requests.post(url, json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "options": {"temperature": 0.3},
            }, timeout=180, proxies={"http": None, "https": None})
            r.raise_for_status()
            return r.json()["message"]["content"].strip()
        except Exception as e:
            logger.warning("Ollama 호출 실패 (%s): %s", url, e)
    return ""


def _embed(text: str) -> list:
    try:
        r = requests.post(EMBED_URL,
                          json={"model": EMBED_MODEL, "prompt": text[:2000]},
                          timeout=30, proxies={"http": None, "https": None})
        return r.json().get("embedding", [])
    except Exception:
        return []


def store_method(insight: str, category: str, sector: str):
    """학습 결과를 chart_method_memory RAG에 저장."""
    try:
        import chromadb
        client = chromadb.PersistentClient(path=CHROMA_DIR)
        col    = client.get_or_create_collection("chart_method_memory")
        text   = f"[기법/업종학습/{sector}/{category}]\n{insight}"
        doc_id = hashlib.md5(f"sector_{sector}_{category}_{insight[:60]}".encode()).hexdigest()
        emb    = _embed(text)
        if not emb:
            logger.warning("임베딩 실패")
            return False
        try:
            col.delete(ids=[doc_id])
        except Exception:
            pass
        col.add(ids=[doc_id], embeddings=[emb], documents=[text],
                metadatas=[{"category": category, "sector": sector,
                            "source": "KIS_10Y", "trust": 1, "applied": 0, "correct": 0}])
        logger.info("RAG 저장: [%s/%s] %s...", sector, category, insight[:60])
        return True
    except Exception as e:
        logger.error("RAG 저장 실패: %s", e)
        return False


def _rows_to_text(rows, limit=25) -> str:
    """DB 레코드 → Ollama 학습용 자연어 텍스트."""
    lines = []
    for r in rows[:limit]:
        (sector, cap, code, name, ptype, date, bc,
         m_ich, m_adx, m_rsi, m_macd,
         w_ich, w_adx, w_rsi, w_macd,
         d_ich, d_adx, d_rsi, d_macd,
         n1, n3, n6) = r

        def yn(v): return "✅" if v else "❌"
        outcome = ""
        if n1 is not None: outcome += f"1개월후{n1:+.1f}% "
        if n3 is not None: outcome += f"3개월후{n3:+.1f}% "
        if n6 is not None: outcome += f"6개월후{n6:+.1f}%"

        lines.append(
            f"[{date[:7]} {name}({cap})] 신호{bc}/12 "
            f"월봉:기준선{yn(m_ich)}ADX{yn(m_adx)}RSI{yn(m_rsi)}MACD{yn(m_macd)} "
            f"주봉:기준선{yn(w_ich)}ADX{yn(w_adx)}RSI{yn(w_rsi)}MACD{yn(w_macd)} "
            f"일봉:기준선{yn(d_ich)}ADX{yn(d_adx)}RSI{yn(d_rsi)}MACD{yn(d_macd)} "
            f"→ {outcome}"
        )
    return "\n".join(lines)


def learn_sector(sector: str, codes: list):
    """업종별 학습 질문 세트 실행."""
    logger.info("\n===== 업종 학습: %s =====", sector)
    con = sqlite3.connect(DB_PATH)

    _COLS = "sector, cap_type, code, name, period_type, date, buy_count, m_ich, m_adx, m_rsi, m_macd, w_ich, w_adx, w_rsi, w_macd, d_ich, d_adx, d_rsi, d_macd, next1_pct, next3_pct, next6_pct"

    # ── 1. 업종 전체 상승 직전 패턴 ──
    rows_up = con.execute(f"""
        SELECT {_COLS} FROM sector_signals
        WHERE sector=? AND next3_pct > 10 AND buy_count >= 6
        ORDER BY RANDOM() LIMIT 30
    """, (sector,)).fetchall()

    rows_dn = con.execute(f"""
        SELECT {_COLS} FROM sector_signals
        WHERE sector=? AND next3_pct < -10 AND buy_count <= 5
        ORDER BY RANDOM() LIMIT 20
    """, (sector,)).fetchall()

    rows_mixed = con.execute(f"""
        SELECT {_COLS} FROM sector_signals
        WHERE sector=? ORDER BY RANDOM() LIMIT 30
    """, (sector,)).fetchall()

    if not rows_up and not rows_mixed:
        con.close()
        logger.warning("학습 데이터 없음: %s", sector)
        return


    # ── 질문 1: 업종 상승 기법 ──
    if rows_up:
        cases_up = _rows_to_text(rows_up)
        prompt1 = f"""아래는 {sector} 업종 한국 주식에서 3개월 내 10% 이상 상승한 직전 데이터야.
월봉/주봉/일봉 기준선·ADX·RSI·MACD 신호가 포함되어 있어.
이 차트에는 기준선(녹색)과 선행스팬2(보라)만 있어. 선행스팬1·후행스팬은 없으니 절대 언급 금지.

{cases_up}

{sector} 업종에서 상승 직전 공통 패턴을 분석해줘:
1. 월봉/주봉/일봉 중 어느 타임프레임 신호가 가장 먼저 켜지나?
2. 기준선과 가격 위치 관계에서 핵심 포인트
3. ADX/RSI/MACD 중 {sector}에서 가장 신뢰도 높은 지표
4. 대형주와 중형주의 신호 패턴 차이 (있으면)
5. 트레이더가 바로 쓸 수 있는 진입 기준 2~3줄

한국어로 간결하게. 실전 매매 기준으로."""

        insight1 = _ask_ollama(prompt1)
        if insight1:
            logger.info("[%s] 상승기법:\n%s\n", sector, insight1[:300])
            store_method(insight1, "상승진입기법", sector)

    # ── 질문 2: 업종 하락 경계 기법 ──
    if rows_dn:
        cases_dn = _rows_to_text(rows_dn)
        prompt2 = f"""아래는 {sector} 업종 한국 주식에서 3개월 내 10% 이상 하락한 직전 데이터야.

{cases_dn}

{sector} 업종에서 하락 전 경고 신호를 분석해줘:
1. 하락 직전 월봉/주봉/일봉에서 공통적으로 꺼지는 신호
2. 기준선 이탈이 실제로 의미있는 타임프레임
3. 매도/관망 타이밍을 알려주는 핵심 지표 조합
4. 이 업종 특유의 하락 전 패턴

한국어로 간결하게. 실전 매도 기준으로."""

        insight2 = _ask_ollama(prompt2)
        if insight2:
            logger.info("[%s] 하락경계:\n%s\n", sector, insight2[:300])
            store_method(insight2, "하락경계기법", sector)

    # ── 질문 3: 타임프레임별 신호 신뢰도 ──
    if rows_mixed:
        cases_mx = _rows_to_text(rows_mixed)
        prompt3 = f"""아래는 {sector} 업종 10년치 다양한 구간 데이터야.

{cases_mx}

{sector} 업종에서 타임프레임별 신호 신뢰도를 분석해줘:
1. 월봉 신호만 켜졌을 때 실제 수익률 패턴
2. 월봉+주봉 정렬됐을 때 vs 월봉만 됐을 때 차이
3. 12신호 중 몇 개 이상일 때 진입 승률이 유의미하게 높아지나?
4. {sector}에서 가장 중요한 단일 신호는?

한국어로 간결하게."""

        insight3 = _ask_ollama(prompt3)
        if insight3:
            logger.info("[%s] 타임프레임:\n%s\n", sector, insight3[:300])
            store_method(insight3, "타임프레임신뢰도", sector)

    # ── 질문 4: 신호 조합별 최적 진입 ──
    rows_hi = con.execute(f"""
        SELECT {_COLS} FROM sector_signals
        WHERE sector=? AND buy_count >= 9 AND next3_pct IS NOT NULL
        ORDER BY next3_pct DESC LIMIT 30
    """, (sector,)).fetchall()

    if rows_hi:
        cases_hi = _rows_to_text(rows_hi)
        prompt4 = f"""아래는 {sector} 업종에서 12신호 중 9개 이상 켜진 고신호 구간 데이터야.

{cases_hi}

고신호(≥9/12) 구간에서 분석해줘:
1. 월봉/주봉/일봉 중 어떤 조합이 9개 이상을 만드는가?
2. 9개 이상일 때 실제 수익률은 6~8개일 때와 얼마나 다른가?
3. 이 업종에서 12개 만점에 가까울수록 과매수 위험인가 vs 강한 추세인가?
4. 고신호에서 진입 타이밍 조언

한국어로 간결하게. 수치 기반으로."""

        insight4 = _ask_ollama(prompt4)
        if insight4:
            logger.info("[%s] 고신호패턴:\n%s\n", sector, insight4[:300])
            store_method(insight4, "고신호진입기법", sector)

    # ── 질문 5: 대형주 vs 중형주 차이 ──
    rows_large = con.execute(f"""
        SELECT {_COLS} FROM sector_signals
        WHERE sector=? AND cap_type='대형' AND next3_pct IS NOT NULL
        ORDER BY RANDOM() LIMIT 20
    """, (sector,)).fetchall()

    rows_mid = con.execute(f"""
        SELECT {_COLS} FROM sector_signals
        WHERE sector=? AND cap_type='중형' AND next3_pct IS NOT NULL
        ORDER BY RANDOM() LIMIT 20
    """, (sector,)).fetchall()

    if rows_large and rows_mid:
        cases_l = _rows_to_text(rows_large, limit=15)
        cases_m = _rows_to_text(rows_mid, limit=15)
        prompt5 = f"""{sector} 업종 대형주 vs 중형주 신호 패턴 비교야.

[대형주]
{cases_l}

[중형주]
{cases_m}

비교 분석:
1. 동일 신호 수에서 대형주와 중형주 중 어느 쪽이 수익률이 높은가?
2. 중형주가 대형주보다 신호에 민감하게 반응하는가?
3. 이 업종에서 대형/중형 선택 기준은?
4. 포트폴리오 배분 관점 조언

한국어로 간결하게."""

        insight5 = _ask_ollama(prompt5)
        if insight5:
            logger.info("[%s] 대형vs중형:\n%s\n", sector, insight5[:300])
            store_method(insight5, "대형중형비교", sector)

    con.close()


def learn_from_trades():
    """C. portfolio.db 실제 거래 결과 기반 학습 — 어떤 신호 패턴에서 수익/손실이 났나."""
    logger.info("\n===== 실거래 결과 학습 =====")
    db_path = os.path.join(os.path.dirname(__file__), "mock_trading", "portfolio.db")
    try:
        con = sqlite3.connect(db_path)
        rows = con.execute("""
            SELECT ticker, name, action, price, qty, buy_signals, rsi, pnl, created_at
            FROM trades
            WHERE action='SELL' AND pnl IS NOT NULL
            ORDER BY created_at DESC
        """).fetchall()
        con.close()
    except Exception as e:
        logger.warning("포트폴리오 DB 조회 실패: %s", e)
        return

    if not rows:
        logger.warning("매도 거래 기록 없음 — 학습 스킵")
        return

    wins = [(r[0], r[1], r[5], r[6], r[7]) for r in rows if r[7] and r[7] > 0]
    losses = [(r[0], r[1], r[5], r[6], r[7]) for r in rows if r[7] and r[7] < 0]

    def fmt(lst):
        return "\n".join(
            f"  {name}({code}) 신호={sig}/12 RSI={rsi:.0f} 손익={pnl:+.1f}%"
            for code, name, sig, rsi, pnl in lst[:20]
        )

    logger.info("수익 거래: %d건 / 손실 거래: %d건", len(wins), len(losses))

    if wins and losses:
        prompt = f"""아래는 실제 자동매매 시스템의 거래 결과야.

[수익 거래 (손익 > 0)]
{fmt(wins)}

[손실 거래 (손익 < 0)]
{fmt(losses)}

이 데이터로 분석해줘:
1. 수익 거래와 손실 거래의 신호 수(12신호) 차이는?
2. RSI 수준에서 수익 vs 손실 패턴 차이
3. 지금 시스템에서 신호≥6 기준이 충분한가, 더 높여야 하나?
4. 손실을 줄이려면 어떤 추가 필터가 필요한가?
5. 이 거래 패턴 기반 개선 제안 3가지

한국어로 간결하게. 실전 개선 방향으로."""

        insight = _ask_ollama(prompt)
        if insight:
            logger.info("실거래학습:\n%s\n", insight[:400])
            store_method(insight, "실거래결과분석", "전체")

    # 수익 TOP5 패턴 별도 학습
    if len(wins) >= 3:
        top = sorted(wins, key=lambda x: x[4], reverse=True)[:5]
        prompt2 = f"""아래는 자동매매에서 가장 수익이 좋았던 거래야.

{fmt(top)}

이 수익 패턴에서:
1. 공통적인 신호 수와 RSI 조합은?
2. 이 종목들의 공통 특징은?
3. 같은 조건을 앞으로 어떻게 포착할 수 있나?

한국어로 짧게."""
        insight2 = _ask_ollama(prompt2)
        if insight2:
            store_method(insight2, "수익패턴TOP", "전체")


def learn_from_backtest():
    """D. backtest_kis.py 실행 → 결과 분석 → RAG 저장."""
    logger.info("\n===== 백테스트 기반 학습 =====")

    # backtest 실행
    import subprocess
    logger.info("백테스트 실행 중 (워치리스트 종목)...")
    try:
        result = subprocess.run(
            ["python3", "/home/ubuntu/-claude-test-/backtest_kis.py"],
            capture_output=True, text=True, timeout=600
        )
        output = result.stdout + result.stderr
        logger.info("백테스트 완료 (%d자)", len(output))
    except subprocess.TimeoutExpired:
        logger.warning("백테스트 타임아웃 (10분)")
        output = ""
    except Exception as e:
        logger.warning("백테스트 실행 실패: %s", e)
        output = ""

    # CSV 결과 읽기
    csv_path = "/home/ubuntu/-claude-test-/backtest_result.csv"
    summary = ""
    try:
        import pandas as pd
        df = pd.read_csv(csv_path)
        df_buy = df[df["buy_count"] >= 6]

        lines = []
        for sig in range(6, 13):
            sub = df[df["buy_count"] >= sig]["r10d"].dropna()
            if len(sub) >= 3:
                wr = (sub > 0).mean() * 100
                avg = sub.mean()
                lines.append(f"  신호≥{sig}: {len(sub)}건 / 승률{wr:.0f}% / 평균{avg:+.1f}%")

        for hd in [5, 10, 20]:
            col = f"r{hd}d"
            if col in df_buy.columns:
                sub = df_buy[col].dropna()
                if len(sub) >= 3:
                    wr = (sub > 0).mean() * 100
                    avg = sub.mean()
                    lines.append(f"  {hd}일보유(신호≥6): {len(sub)}건 / 승률{wr:.0f}% / 평균{avg:+.1f}%")

        summary = "\n".join(lines)
        logger.info("백테스트 요약:\n%s", summary)
    except Exception as e:
        logger.warning("CSV 분석 실패: %s", e)
        summary = output[:2000] if output else "결과 없음"

    if not summary:
        return

    prompt = f"""아래는 한국 주식 워치리스트 종목의 실제 백테스트 결과야 (KIS API 실시간 데이터 기반).

{summary}

이 데이터로 분석해줘:
1. 신호 수가 높을수록 실제로 승률이 높아지는가? (수치 근거)
2. 5일 / 10일 / 20일 중 어떤 보유 기간이 가장 수익률이 좋은가?
3. 현재 신호≥6 매수 기준이 통계적으로 유효한가?
4. 더 높은 임계값(≥8, ≥9)으로 올렸을 때 리스크/리워드 변화
5. 실전 운용에서 최적 신호 임계값과 보유 기간 제안

한국어로 간결하게. 수치 기반으로."""

    insight = _ask_ollama(prompt)
    if insight:
        logger.info("백테스트학습:\n%s\n", insight[:400])
        store_method(insight, "백테스트수익분석", "전체")


SECTOR_PARAMS_PATH = os.path.join(os.path.dirname(__file__), "sector_params.json")


def derive_sector_params():
    """
    Ollama가 학습 데이터를 분석해 업종별 최적 매매 파라미터를 도출.
    결과를 sector_params.json에 저장 → auto_trader.py가 매매 시 로드.

    파라미터:
      min_signal  : 최소 신호 수 (6~12)
      require_both: 외국인+기관 동시 순매수 필수 여부
      max_atr_pct : 허용 최대 일일변동폭 % (초과 시 진입 스킵)
      hold_days   : 권장 보유 기간 (5/10/20)
      note        : Ollama 분석 요약
    """
    logger.info("\n===== 업종별 매매 파라미터 도출 =====")
    con = sqlite3.connect(DB_PATH)
    import re as _re, json as _json

    params = {}
    default = {"min_signal": 6, "require_both": False, "max_atr_pct": 8.0,
                "hold_days": 10, "note": "기본값"}

    for sector in SECTOR_STOCKS.keys():
        # 신호수별 승률 통계 계산
        rows = con.execute("""
            SELECT buy_count, next1_pct, next3_pct, next6_pct
            FROM sector_signals
            WHERE sector=? AND next3_pct IS NOT NULL
        """, (sector,)).fetchall()

        if not rows:
            params[sector] = default.copy()
            continue

        import pandas as pd
        df = pd.DataFrame(rows, columns=["buy_count", "n1", "n3", "n6"])

        stats_lines = []
        for sig in range(4, 13):
            sub = df[df["buy_count"] >= sig]["n3"].dropna()
            if len(sub) >= 5:
                wr = (sub > 0).mean() * 100
                avg = sub.mean()
                stats_lines.append(
                    f"  신호≥{sig}: {len(sub)}건 / 승률{wr:.0f}% / 평균수익{avg:+.1f}%"
                )

        # 보유기간별 비교
        hold_lines = []
        for col, days in [("n1","1개월"), ("n3","3개월"), ("n6","6개월")]:
            sub6 = df[df["buy_count"] >= 6][col].dropna()
            if len(sub6) >= 5:
                wr = (sub6 > 0).mean() * 100
                avg = sub6.mean()
                hold_lines.append(f"  {days}: 승률{wr:.0f}% / 평균{avg:+.1f}%")

        stats_str = "\n".join(stats_lines)
        hold_str  = "\n".join(hold_lines)

        prompt = f"""{sector} 업종 10년 데이터 분석 결과야.

[신호수별 3개월 후 승률]
{stats_str}

[신호≥6 기준 보유기간별 승률]
{hold_str}

이 데이터를 분석해서 {sector} 업종 최적 매매 파라미터를 JSON으로만 반환해줘.

반드시 아래 JSON 형식만 반환 (다른 텍스트 금지):
{{
  "min_signal": 6~12 사이 정수 (승률이 60% 이상 되는 최소 신호 수),
  "require_both": true/false (외국인+기관 동시 순매수 필수 여부 — 바이오/IT처럼 이벤트 드리븐 업종은 true),
  "max_atr_pct": 3.0~15.0 (허용 최대 일일변동폭 % — 변동성 높은 업종은 낮게),
  "hold_days": 5 또는 10 또는 20 (최적 보유 기간),
  "note": "한줄 근거"
}}"""

        raw = _ask_ollama(prompt)
        if not raw:
            params[sector] = default.copy()
            continue

        # JSON 파싱
        try:
            m = _re.search(r'\{[^{}]+\}', raw, _re.DOTALL)
            if m:
                p = _json.loads(m.group())
                # 값 범위 강제
                p["min_signal"]  = max(6, min(12, int(p.get("min_signal", 6))))
                p["require_both"] = bool(p.get("require_both", False))
                p["max_atr_pct"] = max(2.0, min(20.0, float(p.get("max_atr_pct", 8.0))))
                p["hold_days"]   = int(p.get("hold_days", 10))
                p["note"]        = str(p.get("note", ""))[:100]
                params[sector]   = p
                logger.info("[%s] 파라미터: 신호≥%d / both=%s / ATR≤%.1f%% / %d일 | %s",
                            sector, p["min_signal"], p["require_both"],
                            p["max_atr_pct"], p["hold_days"], p["note"])
            else:
                params[sector] = default.copy()
        except Exception as e:
            logger.warning("[%s] JSON 파싱 실패: %s | raw=%s", sector, e, raw[:100])
            params[sector] = default.copy()

    con.close()

    # 파일 저장
    with open(SECTOR_PARAMS_PATH, "w", encoding="utf-8") as f:
        _json.dump(params, f, ensure_ascii=False, indent=2)
    logger.info("sector_params.json 저장 완료: %d개 업종", len(params))

    # RAG에도 요약 저장
    summary = "\n".join(
        f"{s}: 신호≥{p['min_signal']} / 외국인기관동시={p['require_both']} "
        f"/ ATR≤{p['max_atr_pct']}% / {p['hold_days']}일보유 | {p['note']}"
        for s, p in params.items()
    )
    store_method(f"업종별 최적 매매 파라미터 (Ollama 도출):\n{summary}", "업종파라미터", "전체")
    return params


def learn_cross_sector():
    """업종 간 비교 학습."""
    logger.info("\n===== 업종 간 비교 학습 =====")
    con = sqlite3.connect(DB_PATH)

    # 업종별 신호수≥8 승률 계산
    stats = []
    for sector in SECTOR_STOCKS.keys():
        rows = con.execute("""
            SELECT buy_count, next3_pct FROM sector_signals
            WHERE sector=? AND next3_pct IS NOT NULL
        """, (sector,)).fetchall()
        if not rows:
            continue
        df = pd.DataFrame(rows, columns=["buy_count", "next3_pct"])
        high_sig = df[df["buy_count"] >= 8]
        if len(high_sig) < 5:
            continue
        win_rate = (high_sig["next3_pct"] > 0).mean() * 100
        avg_ret  = high_sig["next3_pct"].mean()
        stats.append(f"{sector}: 신호≥8 {len(high_sig)}건 / 승률{win_rate:.0f}% / 평균{avg_ret:+.1f}%")

    con.close()

    if not stats:
        logger.warning("업종간 비교 데이터 없음")
        return

    stats_str = "\n".join(stats)
    prompt = f"""아래는 한국 주식시장 업종별 10년치 신호 시스템 성과야.
신호≥8/12 기준으로 3개월 후 수익률 집계:

{stats_str}

이 데이터를 분석해서:
1. 업종별로 12신호 시스템이 잘 맞는 업종 vs 안 맞는 업종
2. 어떤 업종에서 신호 수가 승률에 가장 큰 영향을 미치나?
3. 업종별로 신호 임계값을 다르게 적용해야 하는가? (예: 반도체는 ≥8, 건설은 ≥6)
4. 업종별 투자 전략 제안

한국어로 간결하게. 실전 포트폴리오 운용 관점으로."""

    insight = _ask_ollama(prompt)
    if insight:
        logger.info("업종간비교:\n%s\n", insight[:400])
        store_method(insight, "업종간비교", "전체")


# ── 메인 ─────────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--collect-only", action="store_true", help="데이터 수집만 (학습 건너뜀)")
    parser.add_argument("--learn-only",   action="store_true", help="학습만 (수집 건너뜀)")
    parser.add_argument("--sector",       type=str, default=None, help="특정 업종만 처리 (예: 반도체)")
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("KIS 10년 업종별 학습 시작")
    logger.info("=" * 60)

    # PC 절전 방지 — 수집/학습 전 구간 슬립 타이머 리셋
    try:
        requests.get("http://localhost:11435/ping_sleep_timer", timeout=5)
        logger.info("초기 슬립 타이머 리셋 완료")
    except Exception:
        pass
    _start_keepalive(interval_sec=270)  # 4분 30초마다 ping (10분 타이머의 절반 이하)

    init_db()

    sectors = {args.sector: SECTOR_STOCKS[args.sector]} if args.sector and args.sector in SECTOR_STOCKS \
              else SECTOR_STOCKS

    # ── 1단계: KIS 데이터 수집 ──
    if not args.learn_only:
        total_records = 0
        for sector, stocks in sectors.items():
            logger.info("\n[ 업종: %s ] 종목 %d개 수집", sector, len(stocks))
            for code, name, cap_type in stocks:
                try:
                    n = process_stock(sector, code, name, cap_type)
                    total_records += n
                    time.sleep(1.0)  # 종목 간 인터벌
                except Exception as e:
                    logger.error("종목 오류 %s(%s): %s", name, code, e)
        logger.info("\n수집 완료: 총 %d건 저장", total_records)

    # ── 2단계: Ollama 학습 ──
    if not args.collect_only:
        # 데이터 있는지 확인
        con = sqlite3.connect(DB_PATH)
        cnt = con.execute("SELECT COUNT(*) FROM sector_signals").fetchone()[0]
        con.close()

        if cnt == 0:
            logger.error("DB에 데이터 없음. --collect-only 먼저 실행하세요.")
            return

        logger.info("\n학습 시작 (%d건 데이터)", cnt)
        for sector in sectors.keys():
            try:
                learn_sector(sector, SECTOR_STOCKS[sector])
                time.sleep(2)
            except Exception as e:
                logger.error("업종 학습 오류 %s: %s", sector, e)

        # 전체 업종 비교 학습
        learn_cross_sector()

        # C. 실거래 결과 학습
        try:
            learn_from_trades()
        except Exception as e:
            logger.error("실거래 학습 오류: %s", e)

        # D. 백테스트 학습
        try:
            learn_from_backtest()
        except Exception as e:
            logger.error("백테스트 학습 오류: %s", e)

        # E. 업종별 최적 파라미터 도출 (핵심)
        try:
            derive_sector_params()
        except Exception as e:
            logger.error("파라미터 도출 오류: %s", e)

        # RAG 최종 확인
        try:
            import chromadb
            client = chromadb.PersistentClient(path=CHROMA_DIR)
            col = client.get_collection("chart_method_memory")
            logger.info("\n최종 chart_method_memory: %d건", col.count())
        except Exception:
            pass

    logger.info("\n전체 완료")


if __name__ == "__main__":
    main()
