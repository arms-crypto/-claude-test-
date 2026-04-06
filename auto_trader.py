#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
auto_trader.py — 자동매매 엔진
_get_auto_mt(), _tg_notify(),
get_volume_surge_top20(), _scrape_naver_codes(), _get_name_by_code(), _get_smart_money_codes(),
_ohlcv_to_df(), _calc_adx(), _ichimoku_signal(), _tf_four_signals(), calculate_chart_signals(), chart_buy_signal(),
is_trading_hours(), sell_mock(), buy_mock(), smart_buy_amount(),
_rag_trade_history(), _ollama_buy_decision(), select_volume_smart_chart(),
_ollama_sell_decision(), auto_trade_cycle(), auto_trade_loop(), _handle_auto_trade_cmd(),
collect_smart_flows(), get_smart_recommendations()
"""

import os
import re
import json
import datetime
import requests
import pandas as pd
import pytz
import ta
from bs4 import BeautifulSoup
from pykrx import stock as pykrx_stock

import config
from db_utils import get_db_pool
from stock_data import _naver_net_buy_list, get_stock_code_from_db
from llm_client import call_mistral_only

logger = config.logger


# ── 싱글턴/알림 헬퍼 ────────────────────────────────────────────────────────

def _get_auto_mt():
    if config._auto_mt_inst is None:
        from mock_trading.mock_trading import MockTrading
        config._auto_mt_inst = MockTrading()
    return config._auto_mt_inst


def _tg_notify(text: str):
    """텔레그램 알림 전송 (비차단)"""
    try:
        requests.post(
            f"https://api.telegram.org/bot{config.TOKEN_RAW}/sendMessage",
            json={"chat_id": config.CHAT_ID, "text": text},
            proxies={"http": None, "https": None},
            timeout=10,
        )
    except Exception:
        logger.exception("_tg_notify 실패")


# ── 거래량 상위 20 ────────────────────────────────────────────────────────────

def get_volume_surge_top20() -> list:
    """네이버 거래량 상위 20 종목 코드 반환"""
    try:
        r = requests.get(
            "https://finance.naver.com/sise/sise_quant.naver",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=8,
            proxies={"http": None, "https": None},
        )
        soup = BeautifulSoup(r.text, "html.parser")
        codes = []
        for a in soup.select('td a[href*="code="]'):
            code = a["href"].split("code=")[-1].split("&")[0]
            if code and len(code) == 6 and code not in codes:
                codes.append(code)
            if len(codes) >= 20:
                break
        logger.info("거래량 상위 %d종목 수집", len(codes))
        return codes
    except Exception:
        logger.exception("get_volume_surge_top20 실패")
        return []


# ── 스마트머니 코드 파싱 ──────────────────────────────────────────────────────

def _scrape_naver_codes(investor_gubun: str, limit: int = 20) -> list:
    """네이버 deal_rank에서 종목코드 scrape. investor_gubun: 9000=외국인, 1000=기관"""
    try:
        url = (f"https://finance.naver.com/sise/sise_deal_rank_iframe.naver"
               f"?sosok=01&investor_gubun={investor_gubun}&type=buy")
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0",
                         "Referer": "https://finance.naver.com/sise/sise_deal_rank.naver"},
                         timeout=10, proxies={"http": None, "https": None})
        from bs4 import BeautifulSoup as _BS
        soup = _BS(r.text, "html.parser")
        codes = []
        for a in soup.select('a[href*="code="]'):
            code = a["href"].split("code=")[-1].split("&")[0]
            if code and len(code) == 6 and code not in codes:
                codes.append(code)
            if len(codes) >= limit:
                break
        return codes
    except Exception:
        return []


def _get_name_by_code(code: str) -> str:
    """종목코드 → 종목명 조회 (DB → naver ETF/stock 순 fallback)"""
    # 1) DB 캐시
    name = get_stock_code_from_db(code)
    if name:
        return name
    # 2) pykrx (개별주)
    try:
        result = pykrx_stock.get_market_ticker_name(code)
        if isinstance(result, str) and result:
            return result
    except Exception:
        pass
    # 3) naver ETF 페이지 fallback
    try:
        r = requests.get(f"https://finance.naver.com/item/main.naver?code={code}",
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=5,
                         proxies={"http": None, "https": None})
        from bs4 import BeautifulSoup as _BS
        soup = _BS(r.text, "html.parser")
        tag = soup.select_one("h2.h_company a") or soup.select_one(".wrap_company h2")
        if tag:
            return tag.get_text(strip=True)
    except Exception:
        pass
    return code


def _get_smart_money_codes() -> list:
    """외국인 순매수 TOP20 + 기관 순매수 TOP20 합산 (ETF 포함)"""
    foreign = _scrape_naver_codes("9000", limit=20)
    inst    = _scrape_naver_codes("1000", limit=20)
    combined = list(dict.fromkeys(foreign + inst))
    logger.info("스마트머니 외국인%d + 기관%d = 합산%d종목", len(foreign), len(inst), len(combined))
    return combined


# ── 차트 신호 계산 ───────────────────────────────────────────────────────────

def _ohlcv_to_df(rows: list):
    """KIS OHLCV 리스트 → pandas DataFrame (close/high/low 컬럼)"""
    if not rows:
        return None
    df = pd.DataFrame(rows)
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=["close", "high", "low"])


def _calc_adx(df, window=14):
    """DataFrame에서 ADX/PDI/MDI 계산. 부족하면 None 반환."""
    if df is None or len(df) < window + 1:
        return None, None, None
    try:
        ind = ta.trend.ADXIndicator(df["high"], df["low"], df["close"], window=window)
        return float(ind.adx().iloc[-1]), float(ind.adx_pos().iloc[-1]), float(ind.adx_neg().iloc[-1])
    except Exception:
        return None, None, None


def _ichimoku_signal(c: "pd.Series") -> bool:
    """일목균형표 — 주가 > 선행스팬1 > 선행스팬2 (완전 상승 배열) 확인.
    선행스팬1 = (전환선9 + 기준선26) / 2
    선행스팬2 = (52일 고+저) / 2
    """
    if len(c) < 52:
        return False
    tenkan   = (c.rolling(9).max()  + c.rolling(9).min())  / 2
    kijun    = (c.rolling(26).max() + c.rolling(26).min()) / 2
    senkou_a = (tenkan + kijun) / 2
    senkou_b = (c.rolling(52).max() + c.rolling(52).min()) / 2
    price    = float(c.iloc[-1])
    return price > float(senkou_a.iloc[-1]) > float(senkou_b.iloc[-1])


def _ma_signals(df) -> dict:
    """이동평균선 정배열 신호. MA5/20/60/120 값과 정배열 여부 반환."""
    result = {"ma5": None, "ma20": None, "ma60": None, "ma120": None,
              "정배열": False, "가격위치": False}
    if df is None or len(df) < 5:
        return result
    c = df["close"]
    try:
        if len(c) >= 5:   result["ma5"]   = round(float(c.rolling(5).mean().iloc[-1]), 0)
        if len(c) >= 20:  result["ma20"]  = round(float(c.rolling(20).mean().iloc[-1]), 0)
        if len(c) >= 60:  result["ma60"]  = round(float(c.rolling(60).mean().iloc[-1]), 0)
        if len(c) >= 120: result["ma120"] = round(float(c.rolling(120).mean().iloc[-1]), 0)
        ma5, ma20, ma60 = result["ma5"], result["ma20"], result["ma60"]
        if ma5 and ma20 and ma60:
            result["정배열"] = bool(ma5 > ma20 > ma60)
        if ma20:
            result["가격위치"] = bool(float(c.iloc[-1]) > ma20)
    except Exception:
        pass
    return result


def _tf_four_signals(df, label: str, signals: dict):
    """
    단일 타임프레임 DataFrame → 4개 지표 계산 후 signals dict에 기록.
    지표: 일목균형표(9/26/52) | ADX(3) PDI>MDI | RSI(6) 30~70 | MACD(5,13,6) > 0
    """
    if df is None or len(df) < 6:
        signals[f"{label}_일목균형표"] = signals[f"{label}_ADX"] = \
        signals[f"{label}_RSI"]       = signals[f"{label}_MACD"] = False
        signals[f"{label}_rsi_val"]   = signals[f"{label}_macd_val"] = signals[f"{label}_adx_val"] = None
        return
    c = df["close"]
    signals[f"{label}_일목균형표"] = _ichimoku_signal(c)
    adx_v, pdi_v, mdi_v = _calc_adx(df, 3)
    signals[f"{label}_ADX"]     = bool(adx_v and adx_v > 7 and pdi_v > mdi_v)
    signals[f"{label}_adx_val"] = round(adx_v, 1) if adx_v else None
    try:
        rsi_v = float(ta.momentum.rsi(c, window=6).iloc[-1]) if len(c) >= 7 else None
        signals[f"{label}_RSI"]     = bool(rsi_v and 30 < rsi_v < 70)
        signals[f"{label}_rsi_val"] = round(rsi_v, 1) if rsi_v else None
    except Exception:
        signals[f"{label}_RSI"] = False; signals[f"{label}_rsi_val"] = None
    try:
        mh = float(ta.trend.macd_diff(c, window_fast=5, window_slow=13, window_sign=6).iloc[-1]) \
             if len(c) >= 14 else None
        signals[f"{label}_MACD"]     = bool(mh and mh > 0)
        signals[f"{label}_macd_val"] = round(mh, 2) if mh else None
    except Exception:
        signals[f"{label}_MACD"] = False; signals[f"{label}_macd_val"] = None


def calculate_chart_signals(code: str) -> dict | None:
    """
    멀티타임프레임 신호 (KIS API) — 월/주/일/분봉 각각 4개 지표:
      일목균형표(9/26/52) | ADX(3) | RSI(6) | MACD(5,13,6)
    총 16신호 → Ollama가 종합 판단
    """
    from mock_trading.kis_client import get_ohlcv, get_minute_ohlcv
    signals = {}

    try:
        _tf_four_signals(_ohlcv_to_df(get_ohlcv(code, "M", 80)), "월봉", signals)
    except Exception:
        for k in ["일목균형표","ADX","RSI","MACD"]: signals[f"월봉_{k}"] = False

    try:
        _tf_four_signals(_ohlcv_to_df(get_ohlcv(code, "W", 80)), "주봉", signals)
    except Exception:
        for k in ["일목균형표","ADX","RSI","MACD"]: signals[f"주봉_{k}"] = False

    try:
        df_d = _ohlcv_to_df(get_ohlcv(code, "D", 120))
        _tf_four_signals(df_d, "일봉", signals)
        ma = _ma_signals(df_d)
        signals["일봉_정배열"]   = ma["정배열"]
        signals["일봉_가격위치"] = ma["가격위치"]
        signals["일봉_ma5"]      = ma["ma5"]
        signals["일봉_ma20"]     = ma["ma20"]
        signals["일봉_ma60"]     = ma["ma60"]
        signals["일봉_ma120"]    = ma["ma120"]
    except Exception:
        for k in ["일목균형표","ADX","RSI","MACD"]: signals[f"일봉_{k}"] = False
        for k in ["정배열","가격위치","ma5","ma20","ma60","ma120"]: signals[f"일봉_{k}"] = None

    # 3분봉 — 단타 판단용
    try:
        _tf_four_signals(_ohlcv_to_df(get_minute_ohlcv(code, interval=3, count=80)), "분봉_3분", signals)
    except Exception:
        for k in ["일목균형표","ADX","RSI","MACD"]: signals[f"분봉_3분_{k}"] = False

    # 15/30/60분봉 — 스윙 진입 타이밍용
    swing_scores = {k: 0 for k in ["일목균형표","ADX","RSI","MACD"]}
    for interval, label in [(15,"분봉_15분"), (30,"분봉_30분"), (60,"분봉_60분")]:
        tmp = {}
        try:
            _tf_four_signals(_ohlcv_to_df(get_minute_ohlcv(code, interval=interval, count=80)), label, tmp)
        except Exception:
            for k in ["일목균형표","ADX","RSI","MACD"]: tmp[f"{label}_{k}"] = False
        signals.update(tmp)
        for k in ["일목균형표","ADX","RSI","MACD"]:
            if tmp.get(f"{label}_{k}"): swing_scores[k] += 1

    # 스윙 분봉 합의 (15/30/60 중 2개 이상)
    signals["분봉_일목균형표"] = swing_scores["일목균형표"] >= 2
    signals["분봉_ADX"]       = swing_scores["ADX"]       >= 2
    signals["분봉_RSI"]       = swing_scores["RSI"]       >= 2
    signals["분봉_MACD"]      = swing_scores["MACD"]      >= 2

    tf_keys = [
        "월봉_일목균형표","월봉_ADX","월봉_RSI","월봉_MACD",
        "주봉_일목균형표","주봉_ADX","주봉_RSI","주봉_MACD",
        "일봉_일목균형표","일봉_ADX","일봉_RSI","일봉_MACD",
        "분봉_일목균형표","분봉_ADX","분봉_RSI","분봉_MACD",
    ]
    buy_count = sum(bool(signals.get(k)) for k in tf_keys)

    logger.info(
        "차트신호 %s → %d/16 | 월봉[이치=%s ADX=%s RSI=%s MACD=%s] "
        "주봉[이치=%s ADX=%s RSI=%s MACD=%s] 일봉[이치=%s ADX=%s RSI=%s MACD=%s] "
        "분봉[이치=%s ADX=%s RSI=%s MACD=%s]",
        code, buy_count,
        signals.get("월봉_일목균형표"), signals.get("월봉_ADX"),
        signals.get("월봉_RSI"), signals.get("월봉_MACD"),
        signals.get("주봉_일목균형표"), signals.get("주봉_ADX"),
        signals.get("주봉_RSI"), signals.get("주봉_MACD"),
        signals.get("일봉_일목균형표"), signals.get("일봉_ADX"),
        signals.get("일봉_RSI"), signals.get("일봉_MACD"),
        signals.get("분봉_일목균형표"), signals.get("분봉_ADX"),
        signals.get("분봉_RSI"), signals.get("분봉_MACD"),
    )

    rsi_val   = signals.get("일봉_rsi_val")
    macd_hist = signals.get("일봉_macd_val")
    return {
        "signals":        signals,
        "buy_count":      buy_count,
        "sig_ichimoku":   signals.get("주봉_일목균형표", False),
        "sig_d_ichimoku": signals.get("일봉_일목균형표", False),
        "sig_macd":       signals.get("일봉_MACD", False),
        "sig_rsi":        signals.get("일봉_RSI", False),
        "sig_adx":        signals.get("분봉_ADX", False),
        "sig_monthly":    signals.get("월봉_ADX", False),
        "sig_m15":        signals.get("분봉_15분_ADX", False),
        "sig_m30":        signals.get("분봉_30분_ADX", False),
        "sig_m60":        signals.get("분봉_60분_ADX", False),
        "rsi":            round(rsi_val, 1) if rsi_val else 0,
        "macd_hist":      round(macd_hist, 2) if macd_hist else 0,
        "adx":            signals.get("일봉_adx_val") or 0,
        "monthly_adx":    signals.get("월봉_adx_val") or 0,
    }


def chart_buy_signal(code: str) -> bool:
    sig = calculate_chart_signals(code)
    return sig is not None and sig["buy_count"] >= 8


# ── 장중 시간 체크 (KST) ────────────────────────────────────────────────────

def is_trading_hours() -> bool:
    """평일 정규장(KST 09:00~15:20)만 허용 — 모의투자 NXT 미지원"""
    now = datetime.datetime.now(pytz.timezone("Asia/Seoul"))
    if now.weekday() >= 5:
        return False
    m = now.hour * 60 + now.minute
    return (9 * 60) <= m <= (15 * 60 + 20)


# ── 모의 매도/매수 래퍼 ─────────────────────────────────────────────────────

def sell_mock(code: str, qty: int, reason: str = "") -> str:
    """MockTrading.sell 래퍼. qty=None 이면 전량"""
    mt     = _get_auto_mt()
    pool   = get_db_pool()
    result = mt.sell(code, qty, oracle_pool=pool)
    logger.info("[자동매매] SELL %s qty=%s %s → %s", code, qty, reason, result[:60])
    return result


def buy_mock(code: str, amount: int, sig: dict = None) -> str:
    """MockTrading.buy 래퍼. amount = 매수금액(원), sig = 차트신호 dict"""
    mt   = _get_auto_mt()
    pool = get_db_pool()
    kwargs = {}
    if sig:
        kwargs["buy_signals"] = sig.get("buy_count")
        kwargs["rsi"]         = sig.get("rsi")
        kwargs["macd_hist"]   = sig.get("macd_hist")
    result = mt.buy(code, amount, oracle_pool=pool, **kwargs)
    logger.info("[자동매매] BUY  %s %d원 → %s", code, amount, result[:60])
    return result


def smart_buy_amount(code: str) -> int:
    """
    보유 예수금 ÷ 남은 슬롯 수 → 슬롯당 배정금액.
    1순위: portfolio.db account 테이블 (항상 최신)
    2순위: KIS API (DB 실패 시)
    전일 대비 5% 이상 감소 시 → 70% 보수적 운영
    최소 50,000원 / 최대 5,000,000원 클램프.
    """
    import sqlite3 as _sqlite3
    from mock_trading.kis_client import get_price
    MAX_SLOTS = 7
    cash = 0
    conservative = False

    # 1순위: DB에서 직접 읽기 (KIS API보다 신뢰성 높음)
    db_path = os.path.join(os.path.dirname(__file__), "mock_trading", "portfolio.db")
    try:
        con = _sqlite3.connect(db_path)
        row      = con.execute("SELECT value FROM account WHERE key='cash'").fetchone()
        prev_row = con.execute("SELECT value FROM account WHERE key='prev_day_cash'").fetchone()
        con.close()
        if row:
            cash = float(row[0])
        if prev_row and cash > 0:
            prev_cash = float(prev_row[0])
            if prev_cash > 0 and cash < prev_cash * 0.95:
                conservative = True
                logger.info("smart_buy_amount: 전일(%d) 대비 현재(%d) 5%%+ 감소 → 보수적 70%% 운영",
                            int(prev_cash), int(cash))
    except Exception:
        logger.warning("smart_buy_amount: DB cash 읽기 실패 → KIS API 시도")

    # 2순위: KIS API (DB 실패 또는 cash=0 시)
    if cash <= 0:
        try:
            from mock_trading.kis_client import get_balance
            bal  = get_balance()
            cash = bal.get("cash", 0)
        except Exception:
            pass

    # 보유 슬롯: _get_holdings()로 직접 계산 (항상 정확)
    remain = 1
    try:
        used   = len(_get_auto_mt()._get_holdings())
        remain = max(1, MAX_SLOTS - used)
    except Exception:
        pass

    if cash <= 0:
        cash = 1_000_000  # 최후 폴백 (50만→100만으로 상향)

    effective_cash = cash * 0.7 if conservative else cash
    amount = int(effective_cash / remain)
    amount = max(50_000, min(5_000_000, amount))

    price = get_price(code) or 0
    if price > 0 and amount < price:
        amount = price

    logger.info("smart_buy_amount %s: 예수금%d%s ÷ 슬롯%d = %d원",
                code, int(cash), "(보수적)" if conservative else "", remain, amount)
    return amount


# ── 신규 매수 후보 선정 ──────────────────────────────────────────────────────

def _rag_trade_history(code: str, sig: dict, limit: int = 5) -> str:
    """portfolio.db에서 유사 신호 조건의 과거 거래 결과를 조회해 텍스트로 반환."""
    import sqlite3 as _sqlite3
    db_path = os.path.join(os.path.dirname(__file__), "mock_trading", "portfolio.db")
    try:
        con = _sqlite3.connect(db_path)
        rows = []

        same = con.execute(
            """SELECT action, price, qty, pnl, created_at
               FROM trades WHERE ticker=? AND action='SELL'
               ORDER BY created_at DESC LIMIT ?""",
            [code, limit],
        ).fetchall()
        for r in same:
            rows.append(f"  [{r[4][:10]}] {code} SELL → 손익 {r[3]:+.1f}%" if r[3] is not None
                        else f"  [{r[4][:10]}] {code} SELL (pnl 없음)")

        rsi = sig.get("rsi") or 50
        similar = con.execute(
            """SELECT ticker, rsi, buy_signals, pnl, created_at
               FROM trades WHERE action='SELL' AND pnl IS NOT NULL
                 AND rsi BETWEEN ? AND ?
               ORDER BY created_at DESC LIMIT ?""",
            [rsi - 10, rsi + 10, limit],
        ).fetchall()
        for r in similar:
            rows.append(f"  [{r[4][:10]}] {r[0]} RSI={r[1]} 신호={r[2]}/16 → 손익 {r[3]:+.1f}%")

        con.close()
        if not rows:
            return ""
        return "📚 유사 과거 사례:\n" + "\n".join(rows)
    except Exception:
        return ""


def _classify_trade_type(sig: dict) -> str:
    """
    단타/스윙 자동 분류.
    3분봉 3/4 이상 → 단타 (당일청산, +2%/-1%)
    그 외 → 스윙 (15~60분봉은 진입 타이밍 참고용)
    """
    s = sig.get("signals", {})
    score_3min = sum([
        bool(s.get("분봉_3분_일목균형표")),
        bool(s.get("분봉_3분_ADX")),
        bool(s.get("분봉_3분_RSI")),
        bool(s.get("분봉_3분_MACD")),
    ])
    return "단타" if score_3min >= 3 else "스윙"


def _ollama_buy_decision(code: str, name: str, sig: dict) -> dict:
    """
    PC Ollama(mistral)에게 매수 여부 + 전략 유형 판단 요청.
    반환: {"action": "BUY"|"SKIP", "trade_type": "단타"|"스윙", "reason": str}
    실패 시 폴백: buy_count >= 8
    """
    import re as _re, json as _json

    s = sig.get("signals", {})
    def v(k): return "✅" if s.get(k) else "❌"
    ma5   = s.get("일봉_ma5")  or 0
    ma20  = s.get("일봉_ma20") or 0
    ma60  = s.get("일봉_ma60") or 0
    ma120 = s.get("일봉_ma120") or 0
    정배열  = "✅" if s.get("일봉_정배열")   else "❌"
    가격위치 = "✅" if s.get("일봉_가격위치") else "❌"

    prompt = (
        f"종목: {name}({code}) | 총신호={sig['buy_count']}/16\n\n"

        "주봉 (스윙 방향)\n"
        f"  일목균형표={v('주봉_일목균형표')} | ADX={v('주봉_ADX')} | MACD={v('주봉_MACD')}\n\n"

        "일봉 + 월봉 (추세 확인)\n"
        f"  일봉: 일목균형표={v('일봉_일목균형표')} | ADX={v('일봉_ADX')} | MACD={v('일봉_MACD')}\n"
        f"  월봉: 일목균형표={v('월봉_일목균형표')} | ADX={v('월봉_ADX')} | MACD={v('월봉_MACD')}\n"
        f"  이동평균: MA5={ma5:,.0f} MA20={ma20:,.0f} MA60={ma60:,.0f} MA120={ma120:,.0f}\n"
        f"  정배열={정배열} | 현재가>MA20={가격위치} | MACD히스트={sig['macd_hist']} | ADX={sig['adx']}\n\n"

        "스윙 진입 타이밍 (15/30/60분봉 합의 — 타이밍 참고용, BUY/SKIP 판단에 영향 없음)\n"
        f"  일목균형표={v('분봉_일목균형표')} | ADX={v('분봉_ADX')} | MACD={v('분봉_MACD')}\n\n"
        "단타 신호 (3분봉 — 3/4 이상이면 당일청산 단타)\n"
        f"  일목균형표={v('분봉_3분_일목균형표')} | ADX={v('분봉_3분_ADX')} | MACD={v('분봉_3분_MACD')}\n\n"
        "기타: 외국인+기관 순매수, 거래량 상위 종목\n"
    )
    rag = _rag_trade_history(code, sig)
    if rag:
        prompt += rag + "\n\n"
    trade_type = _classify_trade_type(sig)  # 3분봉 기반 코드 결정 (단타/스윙)
    prompt += (
        "매수 여부를 판단하세요. 스윙은 주봉/일봉/월봉 기준으로 판단하고, 15~60분봉은 진입 타이밍 참고용입니다.\n"
        "JSON만 반환:\n"
        '{"action":"BUY"|"SKIP","reason":"한줄설명"}'
    )
    try:
        resp = call_mistral_only(prompt)
        m = _re.search(r'\{[^{}]*"action"[^{}]*\}', resp, _re.DOTALL)
        if m:
            d = _json.loads(m.group())
            if d.get("action") in ("BUY", "SKIP"):
                d["trade_type"] = trade_type  # trade_type은 항상 코드 기준
                logger.info("Ollama 매수판단 %s: %s [%s] — %s",
                            code, d["action"], d["trade_type"], d.get("reason",""))
                return d
    except Exception:
        logger.warning("Ollama 매수판단 실패 %s, 폴백룰 적용", code)
    return {"action": "SKIP", "trade_type": trade_type, "reason": f"폴백SKIP(신호 {sig['buy_count']}/16, Ollama실패)"}


def select_volume_smart_chart() -> list:
    """
    거래량TOP20 ∩ 스마트머니(외국인+기관) → Ollama 매수판단.
    [(code, sig_dict), ...] 최대 7개 반환.
    """
    vol_top   = get_volume_surge_top20()
    smart_top = set(_get_smart_money_codes())
    candidates = [c for c in vol_top if c in smart_top]
    logger.info("후보 %d종목 (거래량%d ∩ 스마트%d)",
                len(candidates), len(vol_top), len(smart_top))

    # 교집합 0 폴백: 외국인+기관 동시 순매수 종목
    if not candidates:
        foreign = _scrape_naver_codes("9000", limit=20)
        inst_set = set(_scrape_naver_codes("1000", limit=20))
        candidates = [c for c in foreign if c in inst_set][:10]
        logger.info("교집합 0 → 외국인+기관 동시순매수 폴백 %d종목", len(candidates))
    targets = []
    for code in candidates:
        sig = calculate_chart_signals(code)
        if not sig:
            continue
        name = _get_name_by_code(code)
        decision = _ollama_buy_decision(code, name, sig)
        if decision["action"] == "BUY":
            sig["trade_type"] = decision.get("trade_type", "스윙")
            sig["name"] = name
            targets.append((code, sig))
        if len(targets) >= 7:
            break
    return targets


def _sig_changed(code: str, new_sig: dict) -> str | None:
    """
    BUY 결정 당시 신호와 현재 신호를 비교해 핵심 변화를 감지.
    변화 있으면 변화 내용 문자열 반환, 없으면 None.
    """
    prev = config._pending_buys.get(code)
    if not prev:
        return None
    old_s = prev["signals"]
    new_s = new_sig.get("signals", {})
    changes = []
    # 핵심 신호 변화 감지
    for key in ["주봉_일목균형표", "주봉_MACD", "주봉_ADX", "일봉_일목균형표", "일봉_MACD", "일봉_정배열"]:
        old_v = bool(old_s.get(key))
        new_v = bool(new_s.get(key))
        if old_v and not new_v:
            changes.append(f"{key} ✅→❌")
        elif not old_v and new_v:
            changes.append(f"{key} ❌→✅")
    # MACD 히스트 부호 변화
    old_m = prev.get("macd_hist", 0) or 0
    new_m = new_sig.get("macd_hist", 0) or 0
    if old_m > 0 and new_m <= 0:
        changes.append(f"MACD히스트 양→음({new_m:.1f})")
    elif old_m <= 0 and new_m > 0:
        changes.append(f"MACD히스트 음→양({new_m:.1f})")
    return ", ".join(changes) if changes else None


def _check_pending_buys():
    """
    BUY 결정 후 신호 변화 감시 — auto_trade_cycle 매 사이클 호출.
    변화 감지 시 Ollama 재판단 요청 후 텔레그램 알림.
    """
    if not config._pending_buys:
        return
    expired = []
    kst_now = datetime.datetime.now(pytz.timezone("Asia/Seoul"))
    for code, info in list(config._pending_buys.items()):
        # 단타 30분 / 스윙 120분 초과 시 감시 종료
        limit_sec = 1800 if info.get("trade_type") == "단타" else 7200
        if (kst_now - info["time"]).total_seconds() > limit_sec:
            expired.append(code)
            continue
        try:
            new_sig = calculate_chart_signals(code)
            if not new_sig:
                continue
            change = _sig_changed(code, new_sig)
            if not change:
                continue
            name = info["name"]
            logger.info("⚡ 신호변화 감지 %s(%s): %s → Ollama 재판단", name, code, change)
            decision = _ollama_buy_decision(code, name, new_sig)
            msg = (f"⚡ [{name}({code})] 신호 변화 감지!\n"
                   f"변화: {change}\n"
                   f"Ollama 재판단: {decision['action']} [{decision.get('trade_type','')}]\n"
                   f"이유: {decision.get('reason','')}")
            logger.info(msg)  # 텔레그램 실시간 알림 비활성화 — 17시 일괄 보고로 대체
            # 신호 스냅샷 갱신
            config._pending_buys[code]["signals"] = new_sig.get("signals", {})
            config._pending_buys[code]["macd_hist"] = new_sig.get("macd_hist", 0)
            if decision["action"] == "SKIP":
                expired.append(code)  # SKIP 전환 시 감시 종료
        except Exception:
            logger.exception("_check_pending_buys 오류: %s", code)
    for code in expired:
        config._pending_buys.pop(code, None)


# ── Ollama 매도 판단 ─────────────────────────────────────────────────────────

def _ollama_sell_decision(code: str, name: str, pnl: float, qty: int,
                          avg_price: float, current: float,
                          trade_type: str = "스윙") -> dict:
    """
    Ollama에게 매도 여부 + 다음 확인 시각 판단 요청.
    trade_type: "단타"(당일청산, 익절+2%/손절-1%) | "스윙"(익절+5%/손절-3%)
    반환: {"action": "HOLD"|"SELL_PARTIAL"|"SELL_ALL",
           "ratio": 0.0~1.0, "reason": str, "check_after": int(분)}
    실패/타임아웃 시 폴백 룰 적용.
    """
    import re as _re, json as _json

    sig = calculate_chart_signals(code)

    day_range_pct = atr5_pct = 0.0
    try:
        kst_now   = datetime.datetime.now(pytz.timezone("Asia/Seoul"))
        from_str  = (kst_now - datetime.timedelta(days=10)).strftime("%Y%m%d")
        today_str = kst_now.strftime("%Y%m%d")
        df = pykrx_stock.get_market_ohlcv(from_str, today_str, code)
        day_range_pct = (float(df["고가"].iloc[-1]) - float(df["저가"].iloc[-1])) \
                        / float(df["저가"].iloc[-1]) * 100
        atr5_pct = float((df["고가"] - df["저가"]).tail(5).mean()) / current * 100
    except Exception:
        pass

    hold_days = 0
    try:
        d = config._auto_last_trades.get(code, {}).get("date")
        if d:
            hold_days = (datetime.datetime.now(pytz.timezone("Asia/Seoul")).date()
                         - datetime.date.fromisoformat(d)).days
    except Exception:
        pass

    sig_txt = "차트데이터 없음"
    if sig:
        sig_txt = (f"RSI={sig['rsi']} MACD_hist={sig['macd_hist']} "
                   f"ADX={sig.get('adx','?')} PDI={sig.get('pdi','?')} MDI={sig.get('mdi','?')} "
                   f"일목균형표={'✅' if sig['sig_ichimoku'] else '❌'} "
                   f"매수신호={sig['buy_count']}/16")

    rag_sell = _rag_trade_history(code, sig or {})
    kst_time_str = datetime.datetime.now(pytz.timezone("Asia/Seoul")).strftime("%H:%M")
    prompt = (
        f"종목: {name}({code})\n"
        f"현재가: {int(current):,}원 | 평균단가: {int(avg_price):,}원 | 손익: {pnl:+.2f}%\n"
        f"현재시각: {kst_time_str} KST\n"
        f"오늘 변동폭: {day_range_pct:.1f}% | ATR5: {atr5_pct:.1f}%\n"
        f"차트: {sig_txt}\n"
        f"보유일수: {hold_days}일\n"
        + (rag_sell + "\n" if rag_sell else "")
        + f"\n전략: {trade_type}\n"
        + ("\n판단 기준 (단타 — 당일 청산 목표):\n"
           "- 익절 목표 +2%, 손절 -1%\n"
           "- HOLD 시 check_after는 최대 5분\n"
           "- 추세 조금이라도 꺾이면 빠르게 SELL\n"
           "\n"
           if trade_type == "단타" else
           "\n판단 기준 (스윙 — 수일 보유 가능):\n"
           "- 변동성 크면 섣불리 손절 말고 추세 확인\n"
           "- HOLD 시 check_after 5~30분\n"
           "- 추세 꺾이거나 손실 확대 중이면 SELL\n\n")
        + "JSON만 반환:\n"
        '{"action":"HOLD"|"SELL_PARTIAL"|"SELL_ALL",'
        '"ratio":0.3,"check_after":5,"reason":"한줄설명"}'
    )

    try:
        raw = call_mistral_only(
            prompt,
            system="당신은 주식 매매 판단 AI입니다. JSON만 반환하고 다른 텍스트는 쓰지 마세요.",
            use_tools=False
        )
        m = _re.search(r'\{.*?"action".*?\}', raw, _re.S)
        if m:
            d = _json.loads(m.group())
            action      = d.get("action", "HOLD").upper()
            ratio       = float(d.get("ratio", 0.3))
            check_after = int(d.get("check_after", 15))
            reason      = d.get("reason", "")
            if action in ("HOLD", "SELL_PARTIAL", "SELL_ALL"):
                # 단타는 check_after 최대 5분 강제
                if trade_type == "단타":
                    check_after = min(check_after, 5)
                logger.info("Ollama판단 %s(%s) [%s]: %s ratio=%.1f 다음확인=%d분 [%s]",
                            name, code, trade_type, action, ratio, check_after, reason)
                return {"action": action, "ratio": ratio,
                        "check_after": check_after, "reason": reason}
    except Exception:
        logger.warning("Ollama 매도판단 실패 %s — 폴백룰 적용", code)

    # 폴백 규칙
    if trade_type == "단타":
        if pnl >= 2:
            return {"action": "SELL_ALL",  "ratio": 1.0, "check_after": 0, "reason": "폴백 단타 +2% 익절"}
        if pnl <= -1:
            return {"action": "SELL_ALL",  "ratio": 1.0, "check_after": 0, "reason": "폴백 단타 -1% 손절"}
        return     {"action": "HOLD",      "ratio": 0.0, "check_after": 3, "reason": "폴백 단타 유지"}
    else:
        if pnl >= 5:
            return {"action": "SELL_PARTIAL", "ratio": 0.3, "check_after": 10, "reason": "폴백 스윙 +5% 익절"}
        if pnl <= -3:
            return {"action": "SELL_ALL",     "ratio": 1.0, "check_after": 0,  "reason": "폴백 스윙 -3% 손절"}
        return     {"action": "HOLD",         "ratio": 0.0, "check_after": 15, "reason": "폴백 스윙 유지"}


# ── 핵심 매매 사이클 ─────────────────────────────────────────────────────────

def auto_trade_cycle():
    """
    30초마다 실행:
    1. 장외시간 즉시 리턴
    2. 보유종목 → Ollama 매도판단 (변동성+차트+PnL 종합)
    3. 신규매수: 거래량∩순매수→차트2/4 상위 7종목 × smart_buy_amount(예수금비례)
    """
    if not config._auto_enabled or not is_trading_hours():
        return

    # BUY 결정 후 신호 변화 즉시 감시
    _check_pending_buys()

    from mock_trading.kis_client import get_price
    kst_now  = datetime.datetime.now(pytz.timezone("Asia/Seoul"))
    time_str = kst_now.strftime("%H:%M:%S")
    today    = kst_now.date().isoformat()

    # ── 1. 보유종목 관리 ─────────────────────────────────────────────
    try:
        holdings = _get_auto_mt()._get_holdings()
        for code, name, qty, avg_price in holdings:
            try:
                current = get_price(code)
                if not current:
                    continue
                pnl = (current - avg_price) / avg_price * 100

                last       = config._auto_last_trades.get(code, {})
                next_check = last.get("next_check")
                if next_check and kst_now < next_check:
                    remain = int((next_check - kst_now).total_seconds() / 60)
                    logger.debug("⏭ %s(%s) 스킵 — 다음확인 %d분 후", name, code, remain)
                    continue

                trade_type  = config._auto_last_trades.get(code, {}).get("trade_type", "스윙")

                # 단타 15:10 강제 청산
                if trade_type == "단타" and kst_now.hour == 15 and kst_now.minute >= 10:
                    result = sell_mock(code, None, reason="단타 장마감 강제청산")
                    logger.info("🔔 단타 강제청산 %s(%s): %+.1f%%", name, code, pnl)
                    config._daily_trade_log.append(
                        f"{time_str} 🔔 단타강제청산 {name}({code}) {pnl:+.1f}%")
                    config._auto_last_trades[code] = {"time": time_str, "action": "SELL_ALL", "pnl": pnl}
                    continue

                decision    = _ollama_sell_decision(code, name, pnl, qty, avg_price, current,
                                                    trade_type=trade_type)
                action      = decision["action"]
                ratio       = decision.get("ratio", 0.3)
                reason      = decision.get("reason", "")
                check_after = decision.get("check_after", 15)
                next_dt     = kst_now + datetime.timedelta(minutes=check_after)

                if action == "SELL_PARTIAL":
                    sell_qty = max(1, int(qty * ratio))
                    result   = sell_mock(code, sell_qty, reason=f"Ollama: {reason}")
                    emoji    = "🤑" if pnl >= 0 else "🟡"
                    logger.info("%s 부분매도 %s(%s): %+.1f%% %d주 [%s]",
                                emoji, name, code, pnl, sell_qty, reason)
                    config._daily_trade_log.append(
                        f"{time_str} {emoji} 부분매도 {name}({code}) {pnl:+.1f}% {sell_qty}주\n  └ {reason}")
                    config._auto_last_trades[code] = {"time": time_str, "action": "SELL_PARTIAL",
                                               "pnl": pnl, "next_check": next_dt}

                elif action == "SELL_ALL":
                    result = sell_mock(code, None, reason=f"Ollama: {reason}")
                    emoji  = "🔴" if pnl < 0 else "💰"
                    logger.info("%s 전량매도 %s(%s): %+.1f%% [%s]",
                                emoji, name, code, pnl, reason)
                    config._daily_trade_log.append(
                        f"{time_str} {emoji} 전량매도 {name}({code}) {pnl:+.1f}%\n  └ {reason}")
                    config._auto_last_trades[code] = {"time": time_str, "action": "SELL_ALL",
                                               "pnl": pnl, "next_check": None}

                else:  # HOLD
                    logger.info("⏸ HOLD %s(%s): %+.1f%% → %d분 후 재확인 [%s]",
                                name, code, pnl, check_after, reason)
                    config._auto_last_trades[code] = {**last, "next_check": next_dt}
            except Exception:
                logger.exception("보유종목 처리 오류: %s", code)
    except Exception:
        logger.exception("holdings 조회 실패")
        holdings = []

    # ── 2. 신규 매수 (최대 7종목) ────────────────────────────────────
    # 09:00~09:10 KIS API 불안정 구간 — 신규 매수만 차단 (매도/HOLD는 정상 동작)
    if kst_now.hour == 9 and kst_now.minute < 10:
        logger.info("⏳ 09:10 이전 신규매수 대기 중 (KIS API 안정화)")
        return

    try:
        new_targets = select_volume_smart_chart()
        bought = []
        held_codes = {c for c, *_ in holdings}  # 현재 보유 종목 — 재매수 방어
        for code, sig in new_targets:
            last = config._auto_last_trades.get(code, {})
            # 현재 보유 중인 종목 건너뜀 (전날부터 보유 포함)
            if code in held_codes:
                continue
            # 당일 이미 매수했거나 매도한 종목 건너뜀
            if last.get("date") == today and last.get("action") in ("BUY", "SELL_ALL", "SELL_PARTIAL"):
                continue
            try:
                amount = smart_buy_amount(code)
                result = buy_mock(code, amount, sig=sig)
                if "❌" in result:
                    logger.warning("매수 실패 %s: %s", code, result[:60])
                    continue
                trade_type = sig.get("trade_type", "스윙")
                name = sig.get("name", code)
                config._auto_last_trades[code] = {
                    "time": time_str, "action": "BUY", "date": today,
                    "signals": sig["buy_count"], "rsi": sig["rsi"],
                    "trade_type": trade_type,
                }
                # 실제 매수 성공 후에만 신호 감시 등록
                config._pending_buys[code] = {
                    "name": name,
                    "signals": sig.get("signals", {}),
                    "buy_count": sig["buy_count"],
                    "macd_hist": sig["macd_hist"],
                    "time": kst_now,
                    "trade_type": trade_type,
                }
                bought.append(code)
                logger.info("🚀 신규매수 %s(%s) [%s]: %d원", name, code, trade_type, amount)
                config._daily_trade_log.append(
                    f"{time_str} 🟢 신규매수 {name}({code}) [{trade_type}] {amount:,}원\n"
                    f"  └ RSI={sig['rsi']} MACD={sig['macd_hist']} "
                    f"ADX={sig.get('adx','?')} 신호={sig['buy_count']}/16"
                )
            except Exception:
                logger.exception("신규매수 오류: %s", code)
    except Exception:
        logger.exception("신규 후보 탐색 실패")
        bought = []

    logger.info("[자동매매 %s] 보유:%d  신규매수:%s",
                time_str, len(holdings), bought or "없음")


# ── 30초 schedule 루프 ───────────────────────────────────────────────────────

def _restore_today_trades():
    """서버 재시작 시 당일 매수 기록을 portfolio.db에서 복원 → 중복매수 방지."""
    import sqlite3 as _sqlite3
    db_path = os.path.join(os.path.dirname(__file__), "mock_trading", "portfolio.db")
    today = datetime.datetime.now(pytz.timezone("Asia/Seoul")).date().isoformat()
    try:
        con = _sqlite3.connect(db_path)
        rows = con.execute(
            "SELECT ticker, name, action, created_at FROM trades "
            "WHERE action IN ('BUY','SELL') AND created_at >= ? ORDER BY created_at ASC",
            [today]
        ).fetchall()
        con.close()
        for ticker, name, action, created_at in rows:
            db_action = "BUY" if action == "BUY" else "SELL_ALL"
            if ticker not in config._auto_last_trades or db_action == "SELL_ALL":
                config._auto_last_trades[ticker] = {
                    "action": db_action, "date": today,
                    "time": created_at[11:19] if len(created_at) > 10 else "",
                    "trade_type": "스윙",
                }
        if rows:
            logger.info("당일 매매 복원: %d건", len(rows))
    except Exception:
        logger.exception("_restore_today_trades 실패")


def _log_holdings_status():
    """매시 정각 보유종목 수익률을 로그 파일에 기록 — 장중 에이전트가 읽을 수 있게."""
    import sqlite3 as _sqlite3
    from mock_trading.kis_client import get_price
    kst_now  = datetime.datetime.now(pytz.timezone("Asia/Seoul"))
    log_path = os.path.join(os.path.dirname(__file__), "inspect_reports", "holdings_hourly.log")
    try:
        holdings = _get_auto_mt()._get_holdings()
        db_path  = os.path.join(os.path.dirname(__file__), "mock_trading", "portfolio.db")
        con      = _sqlite3.connect(db_path)
        cash_row = con.execute("SELECT value FROM account WHERE key='cash'").fetchone()
        con.close()
        cash = int(float(cash_row[0])) if cash_row else 0
        lines = [f"[{kst_now.strftime('%Y-%m-%d %H:%M')} KST] 보유:{len(holdings)}종목 | 현금:{cash:,}원"]
        total_pnl_pct = 0.0
        for code, name, qty, avg_price in holdings:
            current = get_price(code) or avg_price
            pnl     = (current - avg_price) / avg_price * 100
            total_pnl_pct += pnl
            trade_type = config._auto_last_trades.get(code, {}).get("trade_type", "스윙")
            lines.append(f"  {name}({code}) [{trade_type}] {qty}주 평단{int(avg_price):,} 현재{int(current):,} → {pnl:+.1f}%")
        if holdings:
            lines.append(f"  평균수익률: {total_pnl_pct/len(holdings):+.1f}%")
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        logger.info("holdings_hourly.log 기록 완료: %d종목", len(holdings))
    except Exception:
        logger.exception("_log_holdings_status 실패")


def auto_trade_loop():
    """30초 간격 자동매매 루프 — daemon 스레드"""
    _restore_today_trades()
    logger.info("자동매매 루프 시작 (30초 간격, 장중 KST만 실행)")
    last_hourly_log = None
    last_scan_time  = None   # 매수신호 스캔 마지막 실행 시각 (HH:MM)
    _SCAN_MINUTES   = {30}   # 매 시 30분에 스캔 (09:30, 10:30, 11:30, 13:30, 14:30)
    while True:
        try:
            auto_trade_cycle()
            kst_now = datetime.datetime.now(pytz.timezone("Asia/Seoul"))
            if is_trading_hours():
                # 매시 정각 보유종목 로그
                if kst_now.minute == 0 and last_hourly_log != kst_now.hour:
                    _log_holdings_status()
                    last_hourly_log = kst_now.hour

                # 30분마다 매수신호 스캔 → 텔레그램 브로드캐스트
                tick = f"{kst_now.hour}:{kst_now.minute:02d}"
                if kst_now.minute in _SCAN_MINUTES and last_scan_time != tick:
                    last_scan_time = tick
                    try:
                        result = scan_buy_signals_for_chat()
                        _tg_notify(f"🔍 [{tick} KST] 매수신호 스캔\n\n{result}")
                        logger.info("매수신호 스캔 브로드캐스트 완료")
                    except Exception:
                        logger.exception("매수신호 스캔 브로드캐스트 실패")
        except Exception:
            logger.exception("auto_trade_cycle 예외")
        import time
        time.sleep(30)


# ── /mock 자동매매 명령어 처리 ───────────────────────────────────────────────

def _handle_auto_trade_cmd(text: str) -> str:
    from mock_trading.kis_client import resolve_code
    parts = text.strip().split()
    sub = parts[2] if len(parts) >= 3 else ""

    if sub == "시작":
        config._auto_enabled = True
        return (
            "✅ 자동매매 시작!\n"
            "⏱ 30초 간격, 평일 09:00~15:20\n"
            "익절 +5%(30%매도) / 손절 -3%(전량)\n"
            "신규: 거래량급등 ∩ 외국인순매수 → 차트 2/3\n"
            "매수단위: 예수금 ÷ 남은슬롯 (최소5만/최대500만)\n"
            "최대 보유 7종목"
        )

    if sub == "종료":
        config._auto_enabled = False
        return "⏹ 자동매매 종료됨."

    if sub == "현황":
        status = "ON 🟢" if config._auto_enabled else "OFF 🔴"
        mt = _get_auto_mt()
        holdings = mt._get_holdings()
        lines = [f"🤖 자동매매: {status}", f"보유종목: {len(holdings)}개"]
        if config._auto_last_trades:
            lines.append("\n최근 매매:")
            for code, info in list(config._auto_last_trades.items())[-5:]:
                lines.append(f"  {code}: {info.get('action')} @ {info.get('time')}")
        return "\n".join(lines)

    if sub == "분석" and len(parts) >= 4:
        name_or_code = " ".join(parts[3:])
        code, display = resolve_code(name_or_code)
        if not code:
            return f"❌ 종목 없음: {name_or_code}"
        sig = calculate_chart_signals(code)
        if not sig:
            return f"❌ {code} 지표 계산 실패"
        s = sig.get("signals", {})
        def v(k): return "✅" if s.get(k) else "❌"
        tt = _classify_trade_type(sig)
        return (
            f"📊 {display}({code}) 차트 분석 [{tt}]\n"
            f"  타임프레임 | 일목균형표 | ADX | MACD\n"
            f"  주봉       | {v('주봉_일목균형표')}  | {v('주봉_ADX')} | {v('주봉_MACD')}\n"
            f"  일봉       | {v('일봉_일목균형표')}  | {v('일봉_ADX')} | {v('일봉_MACD')}\n"
            f"  월봉       | {v('월봉_일목균형표')}  | {v('월봉_ADX')} | {v('월봉_MACD')}\n"
            f"  분봉       | {v('분봉_일목균형표')}  | {v('분봉_ADX')} | {v('분봉_MACD')}\n"
            f"MA정배열={v('일봉_정배열')} | 현재가>MA20={v('일봉_가격위치')}\n"
            f"MACD히스트={sig['macd_hist']} | ADX={sig.get('adx','?')} | RSI={sig['rsi']}\n"
            f"총신호: {sig['buy_count']}/16 → {'🟢 BUY 후보' if sig['buy_count'] >= 8 else '🔴 신호 부족'}"
        )

    status = "ON 🟢" if config._auto_enabled else "OFF 🔴"
    return (
        f"🤖 자동매매 ({status})\n\n"
        "명령어:\n"
        "/mock 자동매매 시작\n"
        "/mock 자동매매 종료\n"
        "/mock 자동매매 현황\n"
        "/mock 자동매매 분석 {종목명or코드}"
    )


# ── 스마트머니 수집 + 추천 ───────────────────────────────────────────────────

def collect_smart_flows(date_str=None):
    """네이버 Finance에서 외국인/금융투자(기관proxy) 순매수 상위 수집 후 DB 저장"""
    import datetime as _dt
    kst = pytz.timezone('Asia/Seoul')
    if not date_str:
        d = _dt.datetime.now(kst)
        if d.weekday() == 5:
            d -= _dt.timedelta(days=1)
        elif d.weekday() == 6:
            d -= _dt.timedelta(days=2)
        date_str = d.strftime('%Y%m%d')

    investor_map = {"외국인합계": "9000", "기관합계": "1000"}
    results = []
    for investor_type, gubun in investor_map.items():
        try:
            df = _naver_net_buy_list(gubun, '01', 'buy')
            if df is None or df.empty:
                logger.warning("collect_smart_flows: %s 데이터 없음", investor_type)
                continue
            from stock_data import get_stock_code_from_db, naver_search_code
            for rank, row in enumerate(df.itertuples(), 1):
                name = str(row.종목명)
                amount_mil = int(row.금액) if hasattr(row, '금액') and str(row.금액) not in ('nan','') else 0
                code = get_stock_code_from_db(name) or naver_search_code(name) or name
                results.append({
                    "date_str": date_str,
                    "investor_type": investor_type,
                    "rank_no": rank,
                    "ticker": code,
                    "name": name,
                    "net_buy_amount": amount_mil * 1_000_000
                })
        except Exception:
            logger.exception("collect_smart_flows: %s 수집 실패", investor_type)

    if not results:
        return False, "수집된 데이터 없음"
    p = get_db_pool()
    if not p:
        return False, "DB 연결 실패"
    try:
        with p.acquire() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM mock_smart_flows WHERE date_str = :1", [date_str])
                conn.commit()
                cur.execute("DELETE FROM mock_smart_flows WHERE collected_at < ADD_MONTHS(SYSTIMESTAMP, -6)")
                conn.commit()
                for row in results:
                    cur.execute(
                        "INSERT INTO mock_smart_flows "
                        "(date_str, investor_type, rank_no, ticker, name, net_buy_amount) "
                        "VALUES (:1, :2, :3, :4, :5, :6)",
                        [row["date_str"], row["investor_type"], row["rank_no"],
                         row["ticker"], row["name"], row["net_buy_amount"]]
                    )
                conn.commit()
        logger.info("collect_smart_flows: %d건 저장 (date=%s)", len(results), date_str)
        return True, f"{date_str} 기준 {len(results)}건 저장 완료"
    except Exception:
        logger.exception("collect_smart_flows DB 저장 실패")
        return False, "DB 저장 실패"


def get_smart_recommendations():
    """최근 7일 기관+외국인 중복 순매수 TOP10 추천"""
    p = get_db_pool()
    if not p:
        return None, "DB 연결 실패"
    try:
        with p.acquire() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT ticker, name, "
                    "COUNT(DISTINCT date_str) AS days_count, "
                    "COUNT(DISTINCT investor_type) AS investor_count, "
                    "SUM(net_buy_amount) AS total_net_buy "
                    "FROM mock_smart_flows "
                    "WHERE collected_at >= SYSTIMESTAMP - INTERVAL '7' DAY "
                    "GROUP BY ticker, name "
                    "HAVING COUNT(DISTINCT investor_type) >= 2 "
                    "ORDER BY days_count DESC, total_net_buy DESC "
                    "FETCH FIRST 10 ROWS ONLY"
                )
                rows = cur.fetchall()
                if not rows:
                    cur.execute(
                        "SELECT ticker, name, "
                        "COUNT(DISTINCT date_str) AS days_count, "
                        "COUNT(DISTINCT investor_type) AS investor_count, "
                        "SUM(net_buy_amount) AS total_net_buy "
                        "FROM mock_smart_flows "
                        "WHERE collected_at >= SYSTIMESTAMP - INTERVAL '7' DAY "
                        "GROUP BY ticker, name "
                        "ORDER BY days_count DESC, total_net_buy DESC "
                        "FETCH FIRST 10 ROWS ONLY"
                    )
                    rows = cur.fetchall()
                return rows, None
    except Exception:
        logger.exception("get_smart_recommendations 오류")
        return None, "DB 조회 실패"


# ── 채팅용 차트 분석 ───────────────────────────────────────────────────────────

def analyze_chart_for_chat(query: str) -> str:
    """
    채팅에서 종목명/코드로 차트 기술적 분석 요청 시 호출.
    BUY / HOLD / SELL 판단 + 근거 반환.
    """
    import re as _re, json as _json
    from stock_data import get_stock_code_from_db, naver_search_code

    query = query.strip()

    # 코드 해석
    if len(query) == 6 and query.isdigit():
        code = query
        name = _get_name_by_code(code) or code
    else:
        code = get_stock_code_from_db(query)
        if not code:
            code = naver_search_code(query)
        if not code:
            return f"❌ '{query}' 종목을 찾을 수 없어요. 6자리 코드로 다시 시도해보세요."
        name = query

    sig = calculate_chart_signals(code)
    if not sig:
        return f"❌ {name}({code}) 신호 계산 실패. KIS API 연결을 확인하세요."

    s = sig.get("signals", {})
    def v(k): return "✅" if s.get(k) else "❌"

    signal_summary = (
        f"종목: {name}({code}) | 총신호: {sig['buy_count']}/16\n\n"
        f"월봉: 일목{v('월봉_일목균형표')} ADX{v('월봉_ADX')} RSI{v('월봉_RSI')} MACD{v('월봉_MACD')}\n"
        f"주봉: 일목{v('주봉_일목균형표')} ADX{v('주봉_ADX')} RSI{v('주봉_RSI')} MACD{v('주봉_MACD')}\n"
        f"일봉: 일목{v('일봉_일목균형표')} ADX{v('일봉_ADX')} RSI{v('일봉_RSI')} MACD{v('일봉_MACD')} "
        f"정배열{v('일봉_정배열')} 가격>MA20{v('일봉_가격위치')}\n"
        f"분봉(15/30/60): 일목{v('분봉_일목균형표')} ADX{v('분봉_ADX')} RSI{v('분봉_RSI')} MACD{v('분봉_MACD')}\n"
        f"단타(3분): 일목{v('분봉_3분_일목균형표')} ADX{v('분봉_3분_ADX')} RSI{v('분봉_3분_RSI')} MACD{v('분봉_3분_MACD')}\n"
        f"ADX={sig.get('adx','?'):.1f} MACD히스트={sig.get('macd_hist','?')}\n\n"
    )

    prompt = (
        signal_summary +
        "위 기술적 신호를 종합해서 현재 이 종목을 매수/관망/매도 중 어떻게 판단하는지 한국어로 답하세요.\n"
        "JSON만 반환:\n"
        '{"action":"BUY"|"HOLD"|"SELL","reason":"2~3줄 근거"}'
    )

    try:
        resp = call_mistral_only(prompt, use_tools=False)
        m = _re.search(r'\{[^{}]*"action"[^{}]*\}', resp, _re.DOTALL)
        if m:
            d = _json.loads(m.group())
            action = d.get("action", "HOLD")
            reason = d.get("reason", "")
            emoji = {"BUY": "📈 매수", "HOLD": "⏸ 관망", "SELL": "📉 매도"}.get(action, "⏸ 관망")
            return (
                f"📊 {name}({code}) 차트 분석\n\n"
                f"{signal_summary}"
                f"판단: {emoji}\n"
                f"근거: {reason}"
            )
    except Exception:
        logger.warning("analyze_chart_for_chat Ollama 판단 실패 %s", code)

    # 폴백: 신호 수로 단순 판단
    cnt = sig['buy_count']
    if cnt >= 10:
        action_str = "📈 매수"
    elif cnt >= 6:
        action_str = "⏸ 관망"
    else:
        action_str = "📉 매도"
    return (
        f"📊 {name}({code}) 차트 분석\n\n"
        f"{signal_summary}"
        f"판단: {action_str} (신호 {cnt}/16 기준)"
    )


def get_watchlist_from_db(months: int = 3) -> list:
    """
    DB mock_smart_flows에서 최근 N개월간 외국인+기관 모두 순매수한 종목 코드 목록 반환.
    등장 횟수 내림차순 정렬.
    """
    p = get_db_pool()
    if not p:
        return []
    try:
        with p.acquire() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT ticker, name, COUNT(DISTINCT date_str) AS days "
                    "FROM mock_smart_flows "
                    "WHERE collected_at >= ADD_MONTHS(SYSTIMESTAMP, :1) "
                    "GROUP BY ticker, name "
                    "HAVING COUNT(DISTINCT investor_type) >= 2 "
                    "ORDER BY days DESC",
                    [-months]
                )
                rows = cur.fetchall()
        # ticker가 6자리 코드인 것만, 아니면 이름으로 코드 조회
        from stock_data import get_stock_code_from_db
        result = []
        seen = set()
        for ticker, name, days in rows:
            code = ticker if (len(ticker) == 6 and ticker.isdigit()) else get_stock_code_from_db(name)
            if code and code not in seen:
                seen.add(code)
                result.append((code, name, days))
        return result
    except Exception:
        logger.exception("get_watchlist_from_db 오류")
        return []


def scan_buy_signals_for_chat(months: int = 3) -> str:
    """
    채팅용 — DB 누적 N개월간 외국인+기관 동시 순매수 종목 워치리스트 기반 매수 신호 스캔.
    오늘 교집합이 없어도 과거 이력이 있으면 유지, N개월간 미등장 시 자동 제외.
    """
    watchlist = get_watchlist_from_db(months)

    # 워치리스트 없으면 오늘 실시간으로 폴백
    if not watchlist:
        foreign = _scrape_naver_codes("9000", limit=20)
        inst_set = set(_scrape_naver_codes("1000", limit=20))
        today_both = [c for c in foreign if c in inst_set]
        candidates = [(c, _get_name_by_code(c) or c, 1) for c in today_both]
    else:
        candidates = watchlist

    if not candidates:
        return "외국인+기관 동시 순매수 워치리스트가 비어있습니다."

    results_buy, results_skip = [], []
    for code, name, days in candidates:
        sig = calculate_chart_signals(code)
        if not sig:
            continue
        decision = _ollama_buy_decision(code, name, sig)
        entry = (code, name, days, sig, decision)
        if decision["action"] == "BUY":
            results_buy.append(entry)
        else:
            results_skip.append(entry)

    lines = [f"📊 외국인+기관 워치리스트 {len(candidates)}종목 스캔 (최근 {months}개월 누적)\n"]

    if results_buy:
        lines.append(f"✅ 매수 신호 ({len(results_buy)}개)")
        for code, name, days, sig, decision in results_buy:
            s  = sig.get("signals", {})
            tt = decision.get("trade_type", "스윙")
            def v(k): return "✅" if s.get(k) else "❌"
            lines.append(
                f"▶ {name}({code}) [{tt}] 신호 {sig['buy_count']}/16 (교집합 {days}일)\n"
                f"  월봉: 일목{v('월봉_일목균형표')} ADX{v('월봉_ADX')} RSI{v('월봉_RSI')} MACD{v('월봉_MACD')}\n"
                f"  주봉: 일목{v('주봉_일목균형표')} ADX{v('주봉_ADX')} RSI{v('주봉_RSI')} MACD{v('주봉_MACD')}\n"
                f"  일봉: 일목{v('일봉_일목균형표')} ADX{v('일봉_ADX')} RSI{v('일봉_RSI')} MACD{v('일봉_MACD')} 정배열{v('일봉_정배열')}\n"
                f"  판단: {decision.get('reason','')}"
            )
    else:
        lines.append("✅ 매수 신호 없음")

    if results_skip:
        lines.append(f"\n⏸ 관망 ({len(results_skip)}개): " +
                     ", ".join(f"{name}({code}) {days}일" for code, name, days, _, __ in results_skip))

    return "\n".join(lines)
