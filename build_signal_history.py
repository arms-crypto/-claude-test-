#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_signal_history.py — 차트 학습 데이터 구축
코스피100 + 코스닥50 대형주 × 10년치 일봉 데이터
→ 가격-일목균형 라인 상관관계 원시값 그대로 계산
→ 상승/하락 구간 패턴 자동 레이블링
→ RAG chart_pattern_memory 저장
→ Ollama가 스스로 패턴 파악

핵심 feature (가격-일목 상관관계):
  각 타임프레임(월/주/일)에서:
  - price_vs_kijun  : 가격 vs 기준선 (+1 위 / -1 아래 / 0 근접)
  - price_vs_span1  : 가격 vs 선행스팬1
  - price_vs_span2  : 가격 vs 선행스팬2
  - span1_vs_span2  : 선행스팬1 vs 선행스팬2 (양운/음운)
  - kijun_slope     : 기준선 기울기 (+1 상승 / -1 하락 / 0 횡보)
  + RSI, MACD히스토그램, ADX 원시값

결과 레이블:
  UP_FIRST   — 먼저 상승 후 하락 (스윙 매수 기회)
  DOWN_FIRST — 먼저 하락 후 상승 (진입 대기)
  BREAKOUT   — 상승 후 계속 상승 (추세 추종)
  REVERSAL   — 하락 전환 (매도/관망)
"""

import os
import sys
import time
import json
import logging
import hashlib
import datetime
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import numpy as np
import ta
from pykrx import stock as pykrx_stock

# ── 로깅 ─────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("build_signal_history.log", encoding="utf-8"),
    ]
)
logger = logging.getLogger("build_signal_history")

# ── 설정 ─────────────────────────────────────────────────────────────────────
START_DATE   = "20150101"   # 10년치 시작
END_DATE     = datetime.date.today().strftime("%Y%m%d")
DB_PATH      = os.path.join(os.path.dirname(__file__), "mock_trading", "signal_history.db")
KOSPI_TOP_N  = 100          # 코스피 시총 상위 N
KOSDAQ_TOP_N = 50           # 코스닥 시총 상위 N
MAX_WORKERS  = 4            # pykrx 동시 호출 (API 부하 방지)
LOOKAHEAD    = 20           # 레이블링 기준 미래 N일

# ── DB 초기화 ─────────────────────────────────────────────────────────────────

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS signal_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            code        TEXT NOT NULL,
            name        TEXT,
            date        TEXT NOT NULL,

            -- 월봉 가격-일목 상관관계
            m_price_vs_kijun  INTEGER,   -- +1/0/-1
            m_price_vs_span1  INTEGER,
            m_price_vs_span2  INTEGER,
            m_span1_vs_span2  INTEGER,   -- +1 양운 / -1 음운
            m_kijun_slope     INTEGER,   -- +1 상승 / -1 하락 / 0 횡보
            m_rsi             REAL,
            m_macd            REAL,
            m_adx             REAL,

            -- 주봉
            w_price_vs_kijun  INTEGER,
            w_price_vs_span1  INTEGER,
            w_price_vs_span2  INTEGER,
            w_span1_vs_span2  INTEGER,
            w_kijun_slope     INTEGER,
            w_rsi             REAL,
            w_macd            REAL,
            w_adx             REAL,

            -- 일봉
            d_price_vs_kijun  INTEGER,
            d_price_vs_span1  INTEGER,
            d_price_vs_span2  INTEGER,
            d_span1_vs_span2  INTEGER,
            d_kijun_slope     INTEGER,
            d_rsi             REAL,
            d_macd            REAL,
            d_adx             REAL,
            d_ma_align        INTEGER,   -- 정배열(+1) / 역배열(-1) / 없음(0)

            -- 결과 레이블
            peak_day    INTEGER,
            peak_pct    REAL,
            trough_day  INTEGER,
            trough_pct  REAL,
            pattern     TEXT,           -- UP_FIRST/DOWN_FIRST/BREAKOUT/REVERSAL

            UNIQUE(code, date)
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_code_date ON signal_history(code, date)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_pattern ON signal_history(pattern)")
    con.commit()
    con.close()
    logger.info("DB 초기화 완료: %s", DB_PATH)


# ── 대형주 목록 수집 ──────────────────────────────────────────────────────────

def _last_business_day() -> str:
    """최근 영업일 날짜 반환 (오늘 포함, pykrx 데이터 있는 날)."""
    d = datetime.date.today()
    for _ in range(10):
        if d.weekday() < 5:  # 월~금
            try:
                df = pykrx_stock.get_market_ohlcv(d.strftime("%Y%m%d"), d.strftime("%Y%m%d"), "005930")
                if df is not None and not df.empty:
                    return d.strftime("%Y%m%d")
            except Exception:
                pass
        d -= datetime.timedelta(days=1)
    # 폴백: 3일 전
    return (datetime.date.today() - datetime.timedelta(days=3)).strftime("%Y%m%d")


def get_large_cap_tickers() -> list:
    """코스피200 + 코스닥150 구성종목 반환 (인덱스 구성종목 직접 조회)."""
    tickers = []

    # 코스피200 구성종목 (인덱스코드 1028)
    try:
        logger.info("코스피200 구성종목 수집...")
        kospi200 = pykrx_stock.get_index_portfolio_deposit_file("1028")
        tickers += list(kospi200)
        logger.info("코스피200 %d종목", len(kospi200))
    except Exception as e:
        logger.error("코스피200 수집 실패: %s", e)
        # 폴백: 전체 목록에서 상위
        try:
            all_k = pykrx_stock.get_market_ticker_list(market="KOSPI")
            tickers += list(all_k)[:KOSPI_TOP_N]
        except Exception:
            pass

    # 코스닥150 구성종목 (인덱스코드 2203)
    try:
        logger.info("코스닥150 구성종목 수집...")
        kosdaq150 = pykrx_stock.get_index_portfolio_deposit_file("2203")
        tickers += list(kosdaq150)
        logger.info("코스닥150 %d종목", len(kosdaq150))
    except Exception as e:
        logger.error("코스닥150 수집 실패: %s", e)
        try:
            all_q = pykrx_stock.get_market_ticker_list(market="KOSDAQ")
            tickers += list(all_q)[:KOSDAQ_TOP_N]
        except Exception:
            pass

    # 중복 제거
    seen = set()
    result = []
    for t in tickers:
        if t not in seen:
            seen.add(t)
            result.append(t)

    logger.info("총 %d종목 수집", len(result))
    return result


def get_ticker_name(code: str) -> str:
    try:
        return pykrx_stock.get_market_ticker_name(code) or code
    except Exception:
        return code


# ── 일봉 수집 (pykrx) ─────────────────────────────────────────────────────────

def fetch_daily_ohlcv(code: str) -> pd.DataFrame | None:
    """pykrx로 10년치 일봉 수집."""
    try:
        df = pykrx_stock.get_market_ohlcv(START_DATE, END_DATE, code)
        if df is None or df.empty:
            return None
        df = df.rename(columns={"시가": "open", "고가": "high", "저가": "low",
                                  "종가": "close", "거래량": "volume"})
        df.index = pd.to_datetime(df.index)
        df = df[df["close"] > 0].copy()
        return df
    except Exception as e:
        logger.warning("일봉 수집 실패 %s: %s", code, e)
        return None


def resample_to_weekly(df: pd.DataFrame) -> pd.DataFrame:
    """일봉 → 주봉 리샘플."""
    try:
        w = df.resample("W-FRI").agg({
            "open": "first", "high": "max", "low": "min",
            "close": "last", "volume": "sum"
        }).dropna()
        return w
    except Exception:
        return pd.DataFrame()


def resample_to_monthly(df: pd.DataFrame) -> pd.DataFrame:
    """일봉 → 월봉 리샘플."""
    try:
        m = df.resample("ME").agg({
            "open": "first", "high": "max", "low": "min",
            "close": "last", "volume": "sum"
        }).dropna()
        return m
    except Exception:
        return pd.DataFrame()


# ── 지표 계산 ─────────────────────────────────────────────────────────────────

def _sign(val: float, thr: float = 0.001) -> int:
    """값의 부호를 +1/0/-1로 반환. thr 이내는 0(근접)."""
    if val > thr: return 1
    if val < -thr: return -1
    return 0


def calc_ichimoku_features(df: pd.DataFrame) -> dict:
    """
    가격-일목 상관관계 원시 feature 계산.
    HTS 파라미터: 전환1/기준1/선행2
    """
    result = {
        "price_vs_kijun": 0, "price_vs_span1": 0, "price_vs_span2": 0,
        "span1_vs_span2": 0, "kijun_slope": 0,
        "rsi": None, "macd": None, "adx": None,
    }
    if df is None or len(df) < 6:
        return result

    h = df["high"]
    l = df["low"]
    c = df["close"]

    # 일목균형표 (HTS: 전환1/기준1/선행2)
    tenkan   = (h.rolling(1).max() + l.rolling(1).min()) / 2
    kijun    = (h.rolling(1).max() + l.rolling(1).min()) / 2
    senkou_a = ((tenkan + kijun) / 2).shift(1)
    senkou_b = ((h.rolling(2).max() + l.rolling(2).min()) / 2).shift(1)

    price = float(c.iloc[-1])
    kijun_now  = float(kijun.iloc[-1])
    span1_now  = float(senkou_a.iloc[-1]) if not pd.isna(senkou_a.iloc[-1]) else kijun_now
    span2_now  = float(senkou_b.iloc[-1]) if not pd.isna(senkou_b.iloc[-1]) else kijun_now

    # 가격 vs 라인 상관관계
    ref = max(abs(price), 1)
    result["price_vs_kijun"] = _sign((price - kijun_now) / ref)
    result["price_vs_span1"] = _sign((price - span1_now) / ref)
    result["price_vs_span2"] = _sign((price - span2_now) / ref)
    result["span1_vs_span2"] = _sign((span1_now - span2_now) / max(abs(span1_now), 1))

    # 기준선 기울기 (5봉 기울기)
    if len(kijun) >= 6:
        kijun_prev = float(kijun.iloc[-6])
        result["kijun_slope"] = _sign((kijun_now - kijun_prev) / max(abs(kijun_prev), 1), thr=0.002)

    # RSI (6기간)
    try:
        rsi_s = ta.momentum.rsi(c, window=6)
        result["rsi"] = round(float(rsi_s.iloc[-1]), 1) if len(rsi_s) >= 7 else None
    except Exception:
        pass

    # MACD 히스토그램 (5/13/6)
    try:
        macd_diff = ta.trend.macd_diff(c, window_fast=5, window_slow=13, window_sign=6)
        result["macd"] = round(float(macd_diff.iloc[-1]), 3) if len(macd_diff) >= 14 else None
    except Exception:
        pass

    # ADX (3기간)
    try:
        if len(df) >= 4 and "high" in df.columns:
            adx_i = ta.trend.ADXIndicator(h, l, c, window=3)
            result["adx"] = round(float(adx_i.adx().iloc[-1]), 1)
    except Exception:
        pass

    return result


def calc_ma_align(df: pd.DataFrame) -> int:
    """이동평균 정배열: +1 / 역배열: -1 / 불명확: 0"""
    try:
        c = df["close"]
        if len(c) < 60:
            return 0
        ma5  = float(c.rolling(5).mean().iloc[-1])
        ma20 = float(c.rolling(20).mean().iloc[-1])
        ma60 = float(c.rolling(60).mean().iloc[-1])
        if ma5 > ma20 > ma60:
            return 1
        if ma5 < ma20 < ma60:
            return -1
        return 0
    except Exception:
        return 0


# ── 레이블링 ──────────────────────────────────────────────────────────────────

def label_pattern(df_daily: pd.DataFrame, idx: int) -> dict:
    """
    idx 이후 LOOKAHEAD일의 가격 움직임으로 패턴 레이블링.
    반환: {peak_day, peak_pct, trough_day, trough_pct, pattern}
    """
    result = {"peak_day": None, "peak_pct": None,
               "trough_day": None, "trough_pct": None, "pattern": None}

    future = df_daily.iloc[idx+1 : idx+1+LOOKAHEAD]
    if len(future) < 5:
        return result

    base_price = float(df_daily["close"].iloc[idx])
    if base_price <= 0:
        return result

    highs = future["high"].values
    lows  = future["low"].values

    peak_pct_arr   = (highs - base_price) / base_price * 100
    trough_pct_arr = (lows  - base_price) / base_price * 100

    peak_day   = int(np.argmax(peak_pct_arr))
    trough_day = int(np.argmin(trough_pct_arr))
    peak_pct   = round(float(peak_pct_arr[peak_day]), 2)
    trough_pct = round(float(trough_pct_arr[trough_day]), 2)

    result["peak_day"]   = peak_day + 1
    result["peak_pct"]   = peak_pct
    result["trough_day"] = trough_day + 1
    result["trough_pct"] = trough_pct

    # 패턴 분류
    if peak_pct >= 8 and peak_day <= 10:
        result["pattern"] = "BREAKOUT"      # 빠르고 강한 상승
    elif trough_pct <= -5 and trough_day < peak_day:
        result["pattern"] = "DOWN_FIRST"    # 먼저 하락 후 상승
    elif peak_day < trough_day and peak_pct >= 3:
        result["pattern"] = "UP_FIRST"      # 먼저 상승 후 하락
    elif peak_pct < 2 and trough_pct <= -3:
        result["pattern"] = "REVERSAL"      # 하락 전환
    else:
        result["pattern"] = "UP_FIRST" if peak_pct > abs(trough_pct) else "DOWN_FIRST"

    return result


# ── RAG 저장 ─────────────────────────────────────────────────────────────────

def _to_arrow(val: int) -> str:
    return "↑" if val == 1 else ("↓" if val == -1 else "→")


def _to_pos(val: int) -> str:
    return "위" if val == 1 else ("아래" if val == -1 else "근접")


def build_rag_text(row: dict) -> str:
    """DB row → RAG 저장 텍스트 변환."""
    lines = [
        f"날짜:{row['date']} 종목:{row['name']}({row['code']})",
        (f"[월봉] 가격-기준선:{_to_pos(row['m_price_vs_kijun'])} "
         f"가격-스팬1:{_to_pos(row['m_price_vs_span1'])} "
         f"가격-스팬2:{_to_pos(row['m_price_vs_span2'])} "
         f"스팬1vs2:{'양운' if row['m_span1_vs_span2']==1 else '음운'} "
         f"기준선:{_to_arrow(row['m_kijun_slope'])} "
         f"RSI:{row['m_rsi']} MACD:{row['m_macd']} ADX:{row['m_adx']}"),
        (f"[주봉] 가격-기준선:{_to_pos(row['w_price_vs_kijun'])} "
         f"가격-스팬1:{_to_pos(row['w_price_vs_span1'])} "
         f"가격-스팬2:{_to_pos(row['w_price_vs_span2'])} "
         f"스팬1vs2:{'양운' if row['w_span1_vs_span2']==1 else '음운'} "
         f"기준선:{_to_arrow(row['w_kijun_slope'])} "
         f"RSI:{row['w_rsi']} MACD:{row['w_macd']} ADX:{row['w_adx']}"),
        (f"[일봉] 가격-기준선:{_to_pos(row['d_price_vs_kijun'])} "
         f"가격-스팬1:{_to_pos(row['d_price_vs_span1'])} "
         f"가격-스팬2:{_to_pos(row['d_price_vs_span2'])} "
         f"스팬1vs2:{'양운' if row['d_span1_vs_span2']==1 else '음운'} "
         f"기준선:{_to_arrow(row['d_kijun_slope'])} "
         f"RSI:{row['d_rsi']} MACD:{row['d_macd']} ADX:{row['d_adx']} "
         f"MA:{'정배열' if row['d_ma_align']==1 else ('역배열' if row['d_ma_align']==-1 else '중립')}"),
        (f"결과:{row['pattern']} "
         f"| 고점:{row['peak_day']}일후+{row['peak_pct']}% "
         f"| 저점:{row['trough_day']}일후{row['trough_pct']}%"),
    ]
    return "\n".join(lines)


def store_to_rag(rows: list):
    """빌드된 row 리스트를 RAG chart_pattern_memory에 저장."""
    try:
        import chromadb
        import requests as _req

        CHROMA_DIR  = os.path.join(os.path.dirname(__file__), "rag_data")
        EMBED_URL   = "http://localhost:11434/api/embeddings"
        EMBED_MODEL = "nomic-embed-text"

        client  = chromadb.PersistentClient(path=CHROMA_DIR)
        col     = client.get_or_create_collection("chart_pattern_memory")

        batch_ids, batch_embs, batch_docs, batch_metas = [], [], [], []
        stored = 0

        for row in rows:
            text   = build_rag_text(row)
            doc_id = hashlib.md5(f"{row['code']}_{row['date']}".encode()).hexdigest()

            try:
                r = _req.post(EMBED_URL,
                              json={"model": EMBED_MODEL, "prompt": text[:2000]},
                              timeout=30,
                              proxies={"http": None, "https": None})
                emb = r.json().get("embedding", [])
            except Exception:
                continue

            if not emb:
                continue

            batch_ids.append(doc_id)
            batch_embs.append(emb)
            batch_docs.append(text)
            batch_metas.append({
                "code": row["code"], "date": row["date"],
                "pattern": row["pattern"] or "",
                "name": row["name"] or "",
            })

            if len(batch_ids) >= 50:
                # 기존 삭제 후 추가 (upsert 대신)
                try:
                    col.delete(ids=batch_ids)
                except Exception:
                    pass
                col.add(ids=batch_ids, embeddings=batch_embs,
                        documents=batch_docs, metadatas=batch_metas)
                stored += len(batch_ids)
                logger.info("RAG 저장 %d건 누적", stored)
                batch_ids, batch_embs, batch_docs, batch_metas = [], [], [], []

        if batch_ids:
            try:
                col.delete(ids=batch_ids)
            except Exception:
                pass
            col.add(ids=batch_ids, embeddings=batch_embs,
                    documents=batch_docs, metadatas=batch_metas)
            stored += len(batch_ids)

        logger.info("RAG 저장 완료: 총 %d건", stored)
        return stored

    except Exception as e:
        logger.error("RAG 저장 실패: %s", e)
        return 0


# ── 종목 처리 ─────────────────────────────────────────────────────────────────

def process_ticker(code: str, name: str) -> list:
    """
    단일 종목 10년치 처리.
    반환: DB/RAG에 저장할 row 리스트
    """
    rows = []

    # 일봉 수집
    df_d = fetch_daily_ohlcv(code)
    if df_d is None or len(df_d) < 60:
        logger.warning("데이터 부족 %s(%s): %d일", code, name,
                        len(df_d) if df_d is not None else 0)
        return rows

    # 주봉/월봉 리샘플
    df_w = resample_to_weekly(df_d)
    df_m = resample_to_monthly(df_d)

    logger.info("처리 중 %s(%s): 일봉%d / 주봉%d / 월봉%d",
                code, name, len(df_d), len(df_w), len(df_m))

    # 일봉 기준으로 각 날짜 처리
    dates = df_d.index.tolist()

    for i, dt in enumerate(dates):
        if i + LOOKAHEAD >= len(dates):
            break  # 미래 데이터 없으면 레이블링 불가

        dt_str = dt.strftime("%Y-%m-%d")

        # 해당 날짜까지의 슬라이스
        df_d_slice = df_d.iloc[:i+1]

        # 주봉: 해당 날짜 이전 주봉
        df_w_slice = df_w[df_w.index <= dt]
        # 월봉: 해당 날짜 이전 월봉
        df_m_slice = df_m[df_m.index <= dt]

        if len(df_d_slice) < 6 or len(df_w_slice) < 4 or len(df_m_slice) < 3:
            continue

        # feature 계산
        m_feat = calc_ichimoku_features(df_m_slice)
        w_feat = calc_ichimoku_features(df_w_slice)
        d_feat = calc_ichimoku_features(df_d_slice)
        ma_align = calc_ma_align(df_d_slice)

        # 레이블링
        label = label_pattern(df_d, i)
        if label["pattern"] is None:
            continue

        row = {
            "code": code, "name": name, "date": dt_str,
            # 월봉
            "m_price_vs_kijun": m_feat["price_vs_kijun"],
            "m_price_vs_span1": m_feat["price_vs_span1"],
            "m_price_vs_span2": m_feat["price_vs_span2"],
            "m_span1_vs_span2": m_feat["span1_vs_span2"],
            "m_kijun_slope":    m_feat["kijun_slope"],
            "m_rsi":            m_feat["rsi"],
            "m_macd":           m_feat["macd"],
            "m_adx":            m_feat["adx"],
            # 주봉
            "w_price_vs_kijun": w_feat["price_vs_kijun"],
            "w_price_vs_span1": w_feat["price_vs_span1"],
            "w_price_vs_span2": w_feat["price_vs_span2"],
            "w_span1_vs_span2": w_feat["span1_vs_span2"],
            "w_kijun_slope":    w_feat["kijun_slope"],
            "w_rsi":            w_feat["rsi"],
            "w_macd":           w_feat["macd"],
            "w_adx":            w_feat["adx"],
            # 일봉
            "d_price_vs_kijun": d_feat["price_vs_kijun"],
            "d_price_vs_span1": d_feat["price_vs_span1"],
            "d_price_vs_span2": d_feat["price_vs_span2"],
            "d_span1_vs_span2": d_feat["span1_vs_span2"],
            "d_kijun_slope":    d_feat["kijun_slope"],
            "d_rsi":            d_feat["rsi"],
            "d_macd":           d_feat["macd"],
            "d_adx":            d_feat["adx"],
            "d_ma_align":       ma_align,
            # 레이블
            **label,
        }
        rows.append(row)

    logger.info("%s(%s) 완료: %d rows", code, name, len(rows))
    return rows


def save_to_db(rows: list):
    """row 리스트를 signal_history DB에 저장."""
    if not rows:
        return 0
    con = sqlite3.connect(DB_PATH)
    stored = 0
    for row in rows:
        try:
            con.execute("""
                INSERT OR REPLACE INTO signal_history (
                    code, name, date,
                    m_price_vs_kijun, m_price_vs_span1, m_price_vs_span2,
                    m_span1_vs_span2, m_kijun_slope, m_rsi, m_macd, m_adx,
                    w_price_vs_kijun, w_price_vs_span1, w_price_vs_span2,
                    w_span1_vs_span2, w_kijun_slope, w_rsi, w_macd, w_adx,
                    d_price_vs_kijun, d_price_vs_span1, d_price_vs_span2,
                    d_span1_vs_span2, d_kijun_slope, d_rsi, d_macd, d_adx,
                    d_ma_align,
                    peak_day, peak_pct, trough_day, trough_pct, pattern
                ) VALUES (
                    :code, :name, :date,
                    :m_price_vs_kijun, :m_price_vs_span1, :m_price_vs_span2,
                    :m_span1_vs_span2, :m_kijun_slope, :m_rsi, :m_macd, :m_adx,
                    :w_price_vs_kijun, :w_price_vs_span1, :w_price_vs_span2,
                    :w_span1_vs_span2, :w_kijun_slope, :w_rsi, :w_macd, :w_adx,
                    :d_price_vs_kijun, :d_price_vs_span1, :d_price_vs_span2,
                    :d_span1_vs_span2, :d_kijun_slope, :d_rsi, :d_macd, :d_adx,
                    :d_ma_align,
                    :peak_day, :peak_pct, :trough_day, :trough_pct, :pattern
                )
            """, row)
            stored += 1
        except Exception as e:
            logger.warning("DB 저장 실패: %s", e)
    con.commit()
    con.close()
    return stored


# ── 통계 요약 → RAG 저장 ─────────────────────────────────────────────────────

def build_pattern_stats_rag():
    """
    signal_history DB에서 패턴별 통계 요약 텍스트 생성 → RAG 저장.
    Ollama가 "이 신호 조합에서 어떤 패턴이 많았나" 바로 참고 가능.
    """
    try:
        import chromadb, requests as _req, hashlib as _hm

        con = sqlite3.connect(DB_PATH)

        # feature 조합별 패턴 통계
        stats_rows = con.execute("""
            SELECT
                m_price_vs_kijun, m_price_vs_span1, m_price_vs_span2,
                w_price_vs_kijun, w_price_vs_span1, w_price_vs_span2,
                d_price_vs_kijun, d_price_vs_span1, d_price_vs_span2,
                d_ma_align,
                pattern,
                COUNT(*) as cnt,
                AVG(peak_pct) as avg_peak,
                AVG(peak_day) as avg_peak_day,
                AVG(trough_pct) as avg_trough
            FROM signal_history
            WHERE pattern IS NOT NULL
            GROUP BY
                m_price_vs_kijun, m_price_vs_span1, m_price_vs_span2,
                w_price_vs_kijun, w_price_vs_span1, w_price_vs_span2,
                d_price_vs_kijun, d_price_vs_span1, d_price_vs_span2,
                d_ma_align, pattern
            HAVING cnt >= 10
            ORDER BY cnt DESC
        """).fetchall()
        con.close()

        EMBED_URL   = "http://localhost:11434/api/embeddings"
        EMBED_MODEL = "nomic-embed-text"
        CHROMA_DIR  = os.path.join(os.path.dirname(__file__), "rag_data")
        client      = chromadb.PersistentClient(path=CHROMA_DIR)
        stat_col    = client.get_or_create_collection("chart_pattern_stats")

        stored = 0
        for r in stats_rows:
            (m_kijun, m_sp1, m_sp2,
             w_kijun, w_sp1, w_sp2,
             d_kijun, d_sp1, d_sp2,
             ma_align, pattern, cnt,
             avg_peak, avg_peak_day, avg_trough) = r

            text = (
                f"[패턴통계] 사례수:{cnt}건 결과:{pattern}\n"
                f"월봉: 기준선{'위' if m_kijun==1 else '아래'} "
                f"스팬1{'위' if m_sp1==1 else '아래'} "
                f"스팬2{'위' if m_sp2==1 else '아래'}\n"
                f"주봉: 기준선{'위' if w_kijun==1 else '아래'} "
                f"스팬1{'위' if w_sp1==1 else '아래'} "
                f"스팬2{'위' if w_sp2==1 else '아래'}\n"
                f"일봉: 기준선{'위' if d_kijun==1 else '아래'} "
                f"스팬1{'위' if d_sp1==1 else '아래'} "
                f"스팬2{'위' if d_sp2==1 else '아래'} "
                f"MA:{'정배열' if ma_align==1 else '역배열'}\n"
                f"평균결과: 고점+{avg_peak:.1f}%({avg_peak_day:.0f}일후) "
                f"저점{avg_trough:.1f}%"
            )

            doc_id = _hm.md5(text[:100].encode()).hexdigest()
            try:
                r_emb = _req.post(EMBED_URL,
                                   json={"model": EMBED_MODEL, "prompt": text},
                                   timeout=30, proxies={"http": None, "https": None})
                emb = r_emb.json().get("embedding", [])
                if emb:
                    try:
                        stat_col.delete(ids=[doc_id])
                    except Exception:
                        pass
                    stat_col.add(ids=[doc_id], embeddings=[emb],
                                  documents=[text],
                                  metadatas=[{"pattern": pattern, "cnt": cnt}])
                    stored += 1
            except Exception as e:
                logger.warning("통계 RAG 저장 실패: %s", e)

        logger.info("패턴 통계 RAG 저장 완료: %d건", stored)
        return stored

    except Exception as e:
        logger.error("통계 RAG 구축 실패: %s", e)
        return 0


# ── 메인 ─────────────────────────────────────────────────────────────────────

def main():
    logger.info("=" * 60)
    logger.info("차트 학습 데이터 구축 시작")
    logger.info("기간: %s ~ %s", START_DATE, END_DATE)
    logger.info("=" * 60)

    # DB 초기화
    init_db()

    # 대형주 목록
    tickers = get_large_cap_tickers()
    if not tickers:
        logger.error("종목 목록 수집 실패")
        sys.exit(1)

    # 이미 처리된 종목 체크
    con = sqlite3.connect(DB_PATH)
    done_codes = set(r[0] for r in con.execute(
        "SELECT DISTINCT code FROM signal_history").fetchall())
    con.close()

    pending = [(c, get_ticker_name(c)) for c in tickers if c not in done_codes]
    logger.info("처리 대상: %d종목 (이미완료: %d)", len(pending), len(done_codes))

    total_rows = 0
    rag_buffer = []

    for i, (code, name) in enumerate(pending):
        logger.info("[%d/%d] %s(%s) 처리 중...", i+1, len(pending), name, code)
        try:
            rows = process_ticker(code, name)
            if rows:
                saved = save_to_db(rows)
                total_rows += saved
                rag_buffer.extend(rows)
                logger.info("  → DB %d건 저장 (누적: %d)", saved, total_rows)

            # RAG는 50종목마다 일괄 저장 (임베딩 API 과부하 방지)
            if len(rag_buffer) >= 5000 or (i+1) % 20 == 0:
                logger.info("RAG 저장 중... (%d rows)", len(rag_buffer))
                store_to_rag(rag_buffer)
                rag_buffer.clear()

            # API 부하 방지
            time.sleep(1)

        except Exception as e:
            logger.error("%s 처리 실패: %s", code, e)
            continue

    # 남은 RAG 버퍼 저장
    if rag_buffer:
        store_to_rag(rag_buffer)

    # 패턴 통계 요약 RAG 저장
    logger.info("패턴 통계 RAG 구축 중...")
    build_pattern_stats_rag()

    logger.info("=" * 60)
    logger.info("완료: 총 %d건 DB 저장", total_rows)
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
