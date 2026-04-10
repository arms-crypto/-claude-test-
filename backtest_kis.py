#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backtest_kis.py — KIS API 일봉 데이터 기반 12신호 백테스트

흐름:
  1. 워치리스트 종목 KIS 일봉 120개 pull
  2. 일봉 → 주봉/월봉 리샘플링
  3. 과거 D일 전 시점으로 슬라이싱 → 12신호 계산
  4. 신호수 ≥6 → 가상 매수 기록
  5. 이후 5/10/20일 실제 수익률 추적
  6. 신호수별(6~12) 승률 / 평균수익 리포트
"""

import sys, os, logging, time
sys.path.insert(0, "/home/ubuntu/-claude-test-")
os.chdir("/home/ubuntu/-claude-test-")

import pandas as pd
import numpy as np
import ta
from concurrent.futures import ThreadPoolExecutor, as_completed

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("backtest_kis.log", encoding="utf-8"),
    ]
)
logger = logging.getLogger("backtest")

# ── 설정 ─────────────────────────────────────────────────────────────────────
LOOKBACK_DAYS  = [5, 10, 15, 20]   # 과거 N영업일 전 시점에서 신호 계산
HOLD_DAYS      = [5, 10, 20]       # 매수 후 N일 보유 수익률
MIN_SIGNAL     = 6                  # 매수 최소 신호수
MAX_WORKERS    = 6                  # 병렬 종목 수 (KIS 레이트 리밋)
DAILY_BARS     = 130                # KIS 일봉 요청 수 (슬라이싱 여유분 포함)


# ── 신호 계산 (auto_trader.py 로직 동일) ────────────────────────────────────

def _ichimoku_signal(df) -> bool:
    """가격 >= 기준선 * 0.99 (HTS 설정)"""
    if df is None or len(df) < 3:
        return False
    c = df["close"]; h = df["high"]; l = df["low"]
    kijun = (h.rolling(1).max() + l.rolling(1).min()) / 2
    price = float(c.iloc[-1])
    kj    = float(kijun.iloc[-1])
    return price >= kj * 0.99


def _four_signals(df) -> dict:
    """4개 지표 계산 — ichimoku / ADX(3) / RSI(6) / MACD(5,13,6)"""
    result = {k: False for k in ["ichimoku", "adx", "rsi", "macd"]}
    if df is None or len(df) < 6:
        return result
    c = df["close"]
    result["ichimoku"] = _ichimoku_signal(df)
    # ADX(3)
    try:
        ind = ta.trend.ADXIndicator(df["high"], df["low"], c, window=3)
        adx = float(ind.adx().iloc[-1])
        pdi = float(ind.adx_pos().iloc[-1])
        mdi = float(ind.adx_neg().iloc[-1])
        result["adx"] = bool(adx > 7 and pdi > mdi)
    except Exception:
        pass
    # RSI(6)
    try:
        rsi = float(ta.momentum.rsi(c, window=6).iloc[-1]) if len(c) >= 7 else None
        result["rsi"] = bool(rsi and rsi > 50)
    except Exception:
        pass
    # MACD(5,13,6)
    try:
        mh = float(ta.trend.macd_diff(c, window_fast=5, window_slow=13, window_sign=6).iloc[-1]) \
             if len(c) >= 14 else None
        result["macd"] = bool(mh and mh > 0)
    except Exception:
        pass
    return result


def _resample_weekly(df_d: pd.DataFrame) -> pd.DataFrame:
    """일봉 DataFrame → 주봉 리샘플링"""
    if df_d is None or len(df_d) < 5:
        return None
    df = df_d.copy()
    df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")
    df = df.set_index("date").sort_index()
    w = df.resample("W").agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum"
    }).dropna(subset=["close"])
    w = w[w["close"] > 0].reset_index()
    w["date"] = w["date"].dt.strftime("%Y%m%d")
    return w if len(w) >= 4 else None


def _resample_monthly(df_d: pd.DataFrame) -> pd.DataFrame:
    """일봉 DataFrame → 월봉 리샘플링"""
    if df_d is None or len(df_d) < 20:
        return None
    df = df_d.copy()
    df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")
    df = df.set_index("date").sort_index()
    m = df.resample("ME").agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum"
    }).dropna(subset=["close"])
    m = m[m["close"] > 0].reset_index()
    m["date"] = m["date"].dt.strftime("%Y%m%d")
    return m if len(m) >= 3 else None


def calc_buy_count(df_d_slice: pd.DataFrame) -> int:
    """일봉 슬라이스 → 주봉/월봉 리샘플 → 12신호 buy_count 계산"""
    df_w = _resample_weekly(df_d_slice)
    df_m = _resample_monthly(df_d_slice)

    s_m = _four_signals(df_m)
    s_w = _four_signals(df_w)
    s_d = _four_signals(df_d_slice)

    count = sum([
        s_m["ichimoku"], s_m["adx"], s_m["rsi"], s_m["macd"],
        s_w["ichimoku"], s_w["adx"], s_w["rsi"], s_w["macd"],
        s_d["ichimoku"], s_d["adx"], s_d["rsi"], s_d["macd"],
    ])
    return count


# ── 종목별 백테스트 ──────────────────────────────────────────────────────────

def backtest_one(code: str, name: str, df_d: pd.DataFrame) -> list:
    """단일 종목 백테스트. 결과 리스트 반환."""
    records = []
    if df_d is None or len(df_d) < 30:
        return records

    prices = df_d["close"].values
    dates  = df_d["date"].values
    n      = len(df_d)

    for lb in LOOKBACK_DAYS:
        # 슬라이싱 — 과거 lb영업일 전까지의 데이터
        if lb >= n - 10:
            continue
        slice_end = n - lb          # 이 시점의 마지막 인덱스
        df_slice  = df_d.iloc[:slice_end].copy()
        if len(df_slice) < 20:
            continue

        buy_count = calc_buy_count(df_slice)
        entry_idx = slice_end       # 매수 시점 인덱스 (다음 봉)
        entry_price = float(prices[entry_idx]) if entry_idx < n else None
        signal_date = dates[slice_end - 1]

        if entry_price is None or entry_price == 0:
            continue

        # 보유 후 수익률
        returns = {}
        for hd in HOLD_DAYS:
            exit_idx = entry_idx + hd
            if exit_idx < n:
                returns[f"r{hd}d"] = round((float(prices[exit_idx]) - entry_price) / entry_price * 100, 2)
            else:
                returns[f"r{hd}d"] = None  # 데이터 없음 (오늘 기준 미래)

        records.append({
            "code":       code,
            "name":       name,
            "date":       signal_date,
            "lookback":   lb,
            "buy_count":  buy_count,
            "entry_price": entry_price,
            **returns,
        })

    return records


# ── 결과 리포트 ───────────────────────────────────────────────────────────────

def print_report(records: list):
    if not records:
        print("결과 없음")
        return

    df = pd.DataFrame(records)
    df_buy = df[df["buy_count"] >= MIN_SIGNAL].copy()

    print(f"\n{'='*65}")
    print(f"  KIS 백테스트 결과  —  총 {len(df)}건 / 매수신호(≥{MIN_SIGNAL}) {len(df_buy)}건")
    print(f"{'='*65}")

    # 신호수별 승률·평균수익 테이블
    print(f"\n📊 신호수별 승률 (10일 보유 기준)")
    print(f"{'신호수':>5} {'건수':>5} {'승률':>7} {'평균수익':>9} {'최대수익':>9} {'최대손실':>9}")
    print("-" * 50)

    for sig in range(6, 13):
        sub = df[df["buy_count"] >= sig]
        sub_r = sub["r10d"].dropna()
        if len(sub_r) == 0:
            continue
        win_rate  = (sub_r > 0).mean() * 100
        avg_r     = sub_r.mean()
        max_r     = sub_r.max()
        min_r     = sub_r.min()
        print(f"  ≥{sig:2d}  {len(sub_r):5d}  {win_rate:6.1f}%  {avg_r:+8.2f}%  {max_r:+8.2f}%  {min_r:+8.2f}%")

    # 보유기간별 비교 (신호≥6 기준)
    print(f"\n📈 보유기간별 평균수익 (신호≥6 기준)")
    for hd in HOLD_DAYS:
        col = f"r{hd}d"
        sub_r = df_buy[col].dropna()
        if len(sub_r) == 0:
            continue
        win_rate = (sub_r > 0).mean() * 100
        avg_r    = sub_r.mean()
        print(f"  {hd:2d}일 보유: 건수={len(sub_r):3d}  승률={win_rate:5.1f}%  평균={avg_r:+.2f}%")

    # 수익 상위 10 종목
    print(f"\n🏆 10일 수익 상위 10건 (신호≥6)")
    top = df_buy.dropna(subset=["r10d"]).nlargest(10, "r10d")[
        ["date", "name", "code", "buy_count", "r10d"]]
    for _, row in top.iterrows():
        print(f"  {row['date']} {row['name']}({row['code']}) {row['buy_count']}/12 → +{row['r10d']:.1f}%")

    # 손실 하위 10 종목
    print(f"\n💔 10일 손실 하위 10건 (신호≥6)")
    bot = df_buy.dropna(subset=["r10d"]).nsmallest(10, "r10d")[
        ["date", "name", "code", "buy_count", "r10d"]]
    for _, row in bot.iterrows():
        print(f"  {row['date']} {row['name']}({row['code']}) {row['buy_count']}/12 → {row['r10d']:.1f}%")

    # 오늘 기준 현재 신호 (lookback=5)
    print(f"\n🔍 최근 신호 현황 (5영업일 전 기준, 신호≥6)")
    recent = df[(df["lookback"] == 5) & (df["buy_count"] >= MIN_SIGNAL)].sort_values("buy_count", ascending=False)
    for _, row in recent.iterrows():
        r5 = f"{row['r5d']:+.1f}%" if pd.notna(row.get("r5d")) else "미래"
        print(f"  {row['name']}({row['code']}) {row['buy_count']}/12  5일수익={r5}")

    print(f"\n{'='*65}\n")


# ── 메인 ─────────────────────────────────────────────────────────────────────

def main():
    from auto_trader import get_watchlist_from_db, _get_name_by_code
    from mock_trading.kis_client import get_ohlcv

    logger.info("=" * 60)
    logger.info("KIS 백테스트 시작")
    logger.info("=" * 60)

    watchlist = get_watchlist_from_db(months=3)
    codes = [(code, name) for code, name, _, _ in watchlist]
    logger.info("워치리스트 %d종목 KIS 일봉 수집 시작...", len(codes))

    # 1. KIS 일봉 데이터 수집 (병렬)
    ohlcv_map = {}

    def _fetch(item):
        code, name = item
        rows = get_ohlcv(code, "D", DAILY_BARS)
        if not rows:
            return None
        df = pd.DataFrame(rows)
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["close", "high", "low"])
        df = df[df["close"] > 0]
        return (code, name, df)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(_fetch, item): item for item in codes}
        for i, fut in enumerate(as_completed(futures)):
            r = fut.result()
            if r:
                code, name, df = r
                ohlcv_map[code] = (name, df)
                logger.info("[%d/%d] %s(%s) %d봉 수집", i+1, len(codes), name, code, len(df))
            time.sleep(0.05)  # 레이트 리밋

    logger.info("수집 완료: %d종목", len(ohlcv_map))

    # 2. 백테스트
    all_records = []
    for code, (name, df_d) in ohlcv_map.items():
        recs = backtest_one(code, name, df_d)
        all_records.extend(recs)

    logger.info("백테스트 완료: 총 %d건", len(all_records))

    # 3. 리포트
    print_report(all_records)

    # 4. CSV 저장
    if all_records:
        out = pd.DataFrame(all_records)
        out.to_csv("backtest_result.csv", index=False, encoding="utf-8-sig")
        logger.info("결과 저장: backtest_result.csv")


if __name__ == "__main__":
    main()
