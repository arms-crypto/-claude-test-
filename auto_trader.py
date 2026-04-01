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

MODEL_ROUTER = {
    "simple":         "gemma3:27b",
    "chart":          "mistral-small:24b",
    "final_decision": "mistral-small:24b",
}


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
    """일목균형표 — 전환선(9) > 기준선(26) 여부만 확인."""
    if len(c) < 26:
        return False
    tenkan = (c.rolling(9).max()  + c.rolling(9).min())  / 2
    kijun  = (c.rolling(26).max() + c.rolling(26).min()) / 2
    return float(tenkan.iloc[-1]) > float(kijun.iloc[-1])


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
    signals[f"{label}_ADX"]     = bool(adx_v and adx_v > 15 and pdi_v > mdi_v)
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
        _tf_four_signals(_ohlcv_to_df(get_ohlcv(code, "D", 120)), "일봉", signals)
    except Exception:
        for k in ["일목균형표","ADX","RSI","MACD"]: signals[f"일봉_{k}"] = False

    min_scores = {k: 0 for k in ["일목균형표","ADX","RSI","MACD"]}
    for interval, label in [(15,"분봉_15분"), (30,"분봉_30분"), (60,"분봉_60분")]:
        tmp = {}
        try:
            _tf_four_signals(_ohlcv_to_df(get_minute_ohlcv(code, interval=interval, count=80)), label, tmp)
        except Exception:
            for k in ["일목균형표","ADX","RSI","MACD"]: tmp[f"{label}_{k}"] = False
        signals.update(tmp)
        for k in ["일목균형표","ADX","RSI","MACD"]:
            if tmp.get(f"{label}_{k}"): min_scores[k] += 1

    signals["분봉_일목균형표"] = min_scores["일목균형표"] >= 2
    signals["분봉_ADX"]       = min_scores["ADX"]       >= 2
    signals["분봉_RSI"]       = min_scores["RSI"]       >= 2
    signals["분봉_MACD"]      = min_scores["MACD"]      >= 2

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
    최소 50,000원 / 최대 5,000,000원 클램프.
    """
    from mock_trading.kis_client import get_balance, get_price
    MAX_SLOTS = 7
    cash = 0
    remain = 1
    try:
        bal      = get_balance()
        cash     = bal.get("cash", 0)
        used     = len(bal.get("holdings", []))
        remain   = max(1, MAX_SLOTS - used)
        amount   = int(cash / remain)
    except Exception:
        amount = 500_000

    amount = max(50_000, min(5_000_000, amount))

    price = get_price(code) or 0
    if price > 0 and amount < price:
        amount = price

    logger.info("smart_buy_amount %s: 예수금%d ÷ 슬롯%d = %d원", code, cash, remain, amount)
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
            rows.append(f"  [{r[4][:10]}] {r[0]} RSI={r[1]} 신호={r[2]}/8 → 손익 {r[3]:+.1f}%")

        con.close()
        if not rows:
            return ""
        return "📚 유사 과거 사례:\n" + "\n".join(rows)
    except Exception:
        return ""


def _ollama_buy_decision(code: str, name: str, sig: dict) -> dict:
    """
    PC Ollama(mistral)에게 매수 여부 판단 요청.
    반환: {"action": "BUY"|"SKIP", "reason": str}
    실패 시 폴백: buy_count >= 5
    """
    import re as _re, json as _json

    prompt = (
        f"종목: {name}({code})\n"
        f"차트 신호 ({sig['buy_count']}/8개 양성):\n"
    )
    s = sig.get("signals", {})
    def v(k): return "✅" if s.get(k) else "❌"
    prompt += (
        f"  타임프레임 | 일목균형표 | ADX(3) | RSI(6) | MACD(5,13,6)\n"
        f"  월봉       |  {v('월봉_일목균형표')}       | {v('월봉_ADX')}    | {v('월봉_RSI')}    | {v('월봉_MACD')}\n"
        f"  주봉       |  {v('주봉_일목균형표')}       | {v('주봉_ADX')}    | {v('주봉_RSI')}    | {v('주봉_MACD')}\n"
        f"  일봉       |  {v('일봉_일목균형표')}       | {v('일봉_ADX')}    | {v('일봉_RSI')}    | {v('일봉_MACD')}\n"
        f"  분봉(합의) |  {v('분봉_일목균형표')}       | {v('분봉_ADX')}    | {v('분봉_RSI')}    | {v('분봉_MACD')}\n"
        f"RSI(일봉)={sig['rsi']} | MACD히스트(일봉)={sig['macd_hist']} | 총신호={sig['buy_count']}/16\n\n"
        "이 종목은 외국인+기관 순매수이며 거래량 상위 종목입니다.\n"
    )
    rag = _rag_trade_history(code, sig)
    if rag:
        prompt += rag + "\n\n"
    prompt += (
        "위 멀티타임프레임 신호와 과거 사례를 종합해 지금 매수할지 판단하세요.\n"
        "JSON만 반환:\n"
        '{"action":"BUY"|"SKIP","reason":"한줄설명"}'
    )
    try:
        resp = call_mistral_only(prompt)
        m = _re.search(r'\{[^{}]*"action"[^{}]*\}', resp, _re.DOTALL)
        if m:
            d = _json.loads(m.group())
            if d.get("action") in ("BUY", "SKIP"):
                logger.info("Ollama 매수판단 %s: %s — %s", code, d["action"], d.get("reason",""))
                return d
    except Exception:
        logger.warning("Ollama 매수판단 실패 %s, 폴백룰 적용", code)
    action = "BUY" if sig["buy_count"] >= 8 else "SKIP"
    return {"action": action, "reason": f"폴백(신호 {sig['buy_count']}/8)"}


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
    targets = []
    for code in candidates:
        sig = calculate_chart_signals(code)
        if not sig:
            continue
        name = _get_name_by_code(code)
        decision = _ollama_buy_decision(code, name, sig)
        if decision["action"] == "BUY":
            targets.append((code, sig))
        if len(targets) >= 7:
            break
    return targets


# ── Ollama 매도 판단 ─────────────────────────────────────────────────────────

def _ollama_sell_decision(code: str, name: str, pnl: float, qty: int,
                          avg_price: float, current: float) -> dict:
    """
    Ollama에게 매도 여부 + 다음 확인 시각 판단 요청.
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
                   f"매수신호={sig['buy_count']}/8")

    rag_sell = _rag_trade_history(code, sig or {})
    prompt = (
        f"종목: {name}({code})\n"
        f"현재가: {int(current):,}원 | 평균단가: {int(avg_price):,}원 | 손익: {pnl:+.2f}%\n"
        f"오늘 변동폭: {day_range_pct:.1f}% | ATR5: {atr5_pct:.1f}%\n"
        f"차트: {sig_txt}\n"
        f"보유일수: {hold_days}일\n"
        + (rag_sell + "\n" if rag_sell else "")
        + "\n판단 기준:\n"
        "- 변동성 크면 섣불리 손절 말고 추세 확인\n"
        "- HOLD라면 몇 분 후 다시 볼지(check_after) 결정\n"
        "  (변동 심하면 5~10분, 안정적이면 20~30분)\n"
        "- 추세 꺾이거나 손실 확대 중이면 SELL\n\n"
        "JSON만 반환:\n"
        '{"action":"HOLD"|"SELL_PARTIAL"|"SELL_ALL",'
        '"ratio":0.3,"check_after":15,"reason":"한줄설명"}'
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
                logger.info("Ollama판단 %s(%s): %s ratio=%.1f 다음확인=%d분 [%s]",
                            name, code, action, ratio, check_after, reason)
                return {"action": action, "ratio": ratio,
                        "check_after": check_after, "reason": reason}
    except Exception:
        logger.warning("Ollama 매도판단 실패 %s — 폴백룰 적용", code)

    if pnl >= 5:
        return {"action": "SELL_PARTIAL", "ratio": 0.3, "check_after": 10, "reason": "폴백 +5% 익절"}
    if pnl <= -3:
        return {"action": "SELL_ALL",     "ratio": 1.0, "check_after": 0,  "reason": "폴백 -3% 손절"}
    return     {"action": "HOLD",         "ratio": 0.0, "check_after": 15, "reason": "폴백 유지"}


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

                decision    = _ollama_sell_decision(code, name, pnl, qty, avg_price, current)
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
    try:
        new_targets = select_volume_smart_chart()
        bought = []
        for code, sig in new_targets:
            last = config._auto_last_trades.get(code, {})
            if last.get("action") == "BUY" and last.get("date") == today:
                continue
            try:
                amount = smart_buy_amount(code)
                result = buy_mock(code, amount, sig=sig)
                config._auto_last_trades[code] = {
                    "time": time_str, "action": "BUY", "date": today,
                    "signals": sig["buy_count"], "rsi": sig["rsi"],
                }
                bought.append(code)
                logger.info("🚀 신규매수 %s: %d원", code, amount)
                if "❌" not in result:
                    config._daily_trade_log.append(
                        f"{time_str} 🟢 신규매수 {code} {amount:,}원\n"
                        f"  └ RSI={sig['rsi']} MACD={sig['macd_hist']} "
                        f"ADX={sig.get('adx','?')} 신호={sig['buy_count']}/8"
                    )
            except Exception:
                logger.exception("신규매수 오류: %s", code)
    except Exception:
        logger.exception("신규 후보 탐색 실패")
        bought = []

    logger.info("[자동매매 %s] 보유:%d  신규매수:%s",
                time_str, len(holdings), bought or "없음")


# ── 30초 schedule 루프 ───────────────────────────────────────────────────────

def auto_trade_loop():
    """30초 간격 자동매매 루프 — daemon 스레드"""
    logger.info("자동매매 루프 시작 (30초 간격, 장중 KST만 실행)")
    while True:
        try:
            auto_trade_cycle()
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
        return (
            f"📊 {display}({code}) 차트 분석\n"
            f"일목: {'✅' if sig['sig_ichimoku'] else '❌'}  "
            f"MACD: {'✅' if sig['sig_macd'] else '❌'}  "
            f"RSI: {'✅' if sig['sig_rsi'] else '❌'}  "
            f"ADX: {'✅' if sig.get('sig_adx') else '❌'}\n"
            f"RSI={sig['rsi']} | MACD히스트={sig['macd_hist']} | "
            f"ADX={sig.get('adx','?')}\n"
            f"매수신호: {sig['buy_count']}/8 → {'🟢 BUY' if sig['buy_count']>=5 else '🔴 HOLD'}"
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
            for rank, row in enumerate(df.itertuples(), 1):
                name = str(row.종목명)
                amount_mil = int(row.금액) if hasattr(row, '금액') and str(row.금액) not in ('nan','') else 0
                results.append({
                    "date_str": date_str,
                    "investor_type": investor_type,
                    "rank_no": rank,
                    "ticker": name,
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
