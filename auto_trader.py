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
import threading

# 비전 분석 후처리 — 베이스 모델 환각 교정 (선행스팬1 없음)
_RE_SPAN1 = re.compile(r'선행스팬\s*1')
_RE_SPAN_1 = re.compile(r'스팬\s*1')
_RE_HUIHAENG = re.compile(r'후행스팬')
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

try:
    import pc_director
except ImportError:
    pc_director = None

try:
    from auxiliary_indicator_evaluator import evaluate_auxiliary_strength, calculate_composite_reliability
except ImportError:
    def evaluate_auxiliary_strength(*args, **kwargs):
        return {"total_strength": 0, "evaluation": "알 수 없음"}
    def calculate_composite_reliability(*args, **kwargs):
        return 50

logger = config.logger

# 학습데이터 파일 쓰기 락 — _call_pc_async 복수 스레드가 동시에 파일을 덮어쓰는 것 방지
_learning_data_lock = threading.Lock()

# PC LLM 호출 제어
_pc_call_cooldown: dict = {}          # code → last_call (datetime)
_pc_daily_calls: int = 0              # 오늘 PC 호출 횟수
_pc_daily_calls_date: str = ""        # 날짜 기반 초기화용 (YYYY-MM-DD)
PC_MAX_DAILY_CALLS: int = 30          # 일일 최대 호출 횟수
PC_SIGNAL_THRESHOLD: int = 2          # 신호 변화 최소 임계값 (절대값)


# ── 싱글턴/알림 헬퍼 ────────────────────────────────────────────────────────

# KY 계좌 텔레그램 설정
_KY_BOT_TOKEN = "8246789875:AAH7M28afnrVvvKF95jFqg7zDYSZD8xqOE0"
_KY_CHAT_ID   = "8647480979"
_KY_DB_PATH   = os.path.join(os.path.dirname(__file__), "mock_trading", "portfolio_ky.db")

_auto_mt_ky_inst = None


def _get_auto_mt():
    """가상 매매 엔진(MockTrading) 싱글턴 인스턴스 반환.

    Returns:
        MockTrading: 가상 포트폴리오 관리 인스턴스 (portfolio.db 사용)
    """
    if config._auto_mt_inst is None:
        from mock_trading.mock_trading import MockTrading
        config._auto_mt_inst = MockTrading()
    return config._auto_mt_inst


def _get_auto_mt_ky():
    """실전 KY 계좌 매매 엔진(MockTrading) 싱글턴 인스턴스 반환.

    Returns:
        MockTrading: 실전 KY 포트폴리오 관리 인스턴스 (portfolio_ky.db 사용, REAL_TRADE=True)
    """
    global _auto_mt_ky_inst
    if _auto_mt_ky_inst is None:
        from mock_trading.mock_trading import MockTrading
        from mock_trading import kis_client_ky
        _auto_mt_ky_inst = MockTrading(db_path=_KY_DB_PATH, kis_module=kis_client_ky)
    return _auto_mt_ky_inst


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


def _tg_notify_ky(text: str):
    """KY 계좌 소유자 텔레그램 알림 전송 (비차단)"""
    try:
        requests.post(
            f"https://api.telegram.org/bot{_KY_BOT_TOKEN}/sendMessage",
            json={"chat_id": _KY_CHAT_ID, "text": text},
            proxies={"http": None, "https": None},
            timeout=10,
        )
    except Exception:
        logger.exception("_tg_notify_ky 실패")


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
    """종목코드 → 종목명 조회 (DB → pykrx ETF → pykrx 주식 → naver 순 fallback)"""
    # 1) DB 캐시
    name = get_stock_code_from_db(code)
    if name:
        return name
    # 2) pykrx ETF 먼저 (KODEX 등 ETF 코드 오류 방지)
    try:
        etf_tickers = pykrx_stock.get_etf_ticker_list()
        if code in etf_tickers:
            result = pykrx_stock.get_etf_ticker_name(code)
            if isinstance(result, str) and result:
                return result
    except Exception:
        pass
    # 3) pykrx 개별주
    try:
        result = pykrx_stock.get_market_ticker_name(code)
        if isinstance(result, str) and result:
            return result
    except Exception:
        pass
    # 4) naver fallback
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


def _ichimoku_signal(df) -> bool:
    """일목균형표 — HTS 설정 기준 (전환1/기준1/선행1=1/선행2=2)
    주가 > 선행스팬1 > 선행스팬2 (상승 배열) 확인.
    선행스팬1 = (전환선1 + 기준선1) / 2  → shift(1)
    선행스팬2 = (2기간 고+저) / 2         → shift(1)
    """
    # df 또는 Series 둘 다 허용 (하위 호환)
    if hasattr(df, "columns"):
        c = df["close"]
        h = df["high"] if "high" in df.columns else c
        l = df["low"]  if "low"  in df.columns else c
    else:
        c = h = l = df  # 구 방식: close만 있는 경우
    if len(c) < 4:
        return False
    tenkan   = (h.rolling(1).max() + l.rolling(1).min()) / 2   # 전환선(1) = (고+저)/2
    kijun    = (h.rolling(1).max() + l.rolling(1).min()) / 2   # 기준선(1) = 동일
    senkou_a = ((tenkan + kijun) / 2).shift(1)                  # 선행스팬1
    senkou_b = ((h.rolling(2).max() + l.rolling(2).min()) / 2).shift(1)  # 선행스팬2
    price = float(c.iloc[-1])
    kj    = float(kijun.iloc[-1])   # 차트에 보이는 기준선(녹색) 현재값
    # 가격이 차트 기준선(녹색) 위에 있는지만 확인 — 시각적 일치
    return price >= kj * 0.99


def _ma_signals(df) -> dict:
    """이동평균선(5/20/60/120) 정배열 신호 계산.

    Args:
        df: pandas DataFrame with 'close' column

    Returns:
        dict: {'ma5', 'ma20', 'ma60', 'ma120', '정배열'(MA5>20>60), '가격위치'(가격>MA20)}
    """
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
    지표: 일목균형표(9/26/52) | ADX(3) PDI>MDI | RSI(6) > 50 | MACD(5,13,6) > 0
    """
    if df is None or len(df) < 6:
        signals[f"{label}_일목균형표"] = signals[f"{label}_ADX"] = \
        signals[f"{label}_RSI"]       = signals[f"{label}_MACD"] = False
        signals[f"{label}_rsi_val"]   = signals[f"{label}_macd_val"] = signals[f"{label}_adx_val"] = None
        return
    c = df["close"]
    signals[f"{label}_일목균형표"] = _ichimoku_signal(df)  # HTS 설정(전환1/기준1/선행2) 적용
    adx_v, pdi_v, mdi_v = _calc_adx(df, 3)
    signals[f"{label}_ADX"]     = bool(adx_v and adx_v > 7 and pdi_v > mdi_v)
    signals[f"{label}_adx_val"] = round(adx_v, 1) if adx_v else None
    try:
        rsi_v = float(ta.momentum.rsi(c, window=6).iloc[-1]) if len(c) >= 7 else None
        signals[f"{label}_RSI"]     = bool(rsi_v and rsi_v > 50)
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


def calculate_chart_signals(code: str, scan_mode: bool = False) -> dict | None:
    """
    멀티타임프레임 신호 (KIS API) — 월/주/일/분봉 각각 4개 지표:
      일목균형표(HTS) | ADX(3) | RSI(6) | MACD(5,13,6)
    scan_mode=True: 분봉 스킵 (월/주/일 3 API 호출만) — 스캔 속도 최적화
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

    if not scan_mode:
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

        signals["분봉_일목균형표"] = swing_scores["일목균형표"] >= 2
        signals["분봉_ADX"]       = swing_scores["ADX"]       >= 2
        signals["분봉_RSI"]       = swing_scores["RSI"]       >= 2
        signals["분봉_MACD"]      = swing_scores["MACD"]      >= 2
    else:
        # scan_mode: 분봉 스킵 — 0으로 채움
        for k in ["일목균형표","ADX","RSI","MACD"]:
            signals[f"분봉_3분_{k}"] = False
            signals[f"분봉_{k}"]     = False

    # 스윙 판단용 12신호 (월봉/주봉/일봉) — 분봉은 단타 타이밍용으로 별도
    swing_keys = [
        "월봉_일목균형표","월봉_ADX","월봉_RSI","월봉_MACD",
        "주봉_일목균형표","주봉_ADX","주봉_RSI","주봉_MACD",
        "일봉_일목균형표","일봉_ADX","일봉_RSI","일봉_MACD",
    ]
    minute_keys = ["분봉_일목균형표","분봉_ADX","분봉_RSI","분봉_MACD"]
    buy_count   = sum(bool(signals.get(k)) for k in swing_keys)   # /12
    minute_count = sum(bool(signals.get(k)) for k in minute_keys) # /4 (참고용)

    logger.info(
        "차트신호 %s → %d/12 (분봉%d/4) | 월봉[이치=%s ADX=%s RSI=%s MACD=%s] "
        "주봉[이치=%s ADX=%s RSI=%s MACD=%s] 일봉[이치=%s ADX=%s RSI=%s MACD=%s] "
        "분봉[이치=%s ADX=%s RSI=%s MACD=%s]",
        code, buy_count, minute_count,
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

    # 거래량 비율: 당일 거래량 / 20일 평균 거래량
    volume_ratio = 1.0
    if 'df_d' in locals() and df_d is not None and len(df_d) >= 20:
        try:
            vol_ma20 = df_d['volume'].rolling(20).mean().iloc[-1]
            if vol_ma20 and vol_ma20 > 0:
                volume_ratio = float(df_d['volume'].iloc[-1]) / float(vol_ma20)
        except Exception:
            pass

    return {
        "signals":        signals,
        "buy_count":      buy_count,       # 스윙 판단 /12
        "minute_count":   minute_count,    # 단타 타이밍 참고 /4
        "volume_ratio":   round(volume_ratio, 2),  # 당일거래량 / 20일평균거래량
        "df_daily":       df_d if 'df_d' in locals() else None,  # 차트 PNG 생성용
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
    """차트 신호 기반 매수 신호 판정 (12신호 중 6개 이상).

    Args:
        code (str): 종목코드

    Returns:
        bool: 매수 신호 여부 (buy_count >= 6/12)
    """
    sig = calculate_chart_signals(code)
    return sig is not None and sig["buy_count"] >= 6  # /12 기준 50%+


# ── 장중 시간 체크 (KST) ────────────────────────────────────────────────────

def is_trading_hours() -> bool:
    """평일 KST 08:00~20:00 허용 (NXT 08~20 + KRX 정규장 09~15:30 포함)."""
    now = datetime.datetime.now(pytz.timezone("Asia/Seoul"))
    if now.weekday() >= 5:
        return False
    m = now.hour * 60 + now.minute
    return (8 * 60) <= m < (20 * 60)


def is_nxt_hours() -> bool:
    """NXT 시간 여부 — 정규장 제외 시간 (08:00~09:00, 15:30~20:00)."""
    now = datetime.datetime.now(pytz.timezone("Asia/Seoul"))
    if now.weekday() >= 5:
        return False
    m = now.hour * 60 + now.minute
    return (8 * 60) <= m < (9 * 60) or (15 * 60 + 30) <= m < (20 * 60)


# ── 모의 매도/매수 래퍼 ─────────────────────────────────────────────────────

def sell_mock(code: str, qty: int, reason: str = "") -> str:
    """트레이너 계좌 매도 래퍼. qty=None 이면 전량."""
    mt     = _get_auto_mt()
    pool   = get_db_pool()
    result = mt.sell(code, qty, oracle_pool=pool)
    logger.info("[자동매매] SELL %s qty=%s %s → %s", code, qty, reason, result[:60])
    # 피드백 루프: 익절이면 기법 신뢰도 UP, 손절이면 DOWN
    try:
        from learn_chart_method import update_method_trust
        correct = "익절" in reason or "목표" in reason
        update_method_trust("종합_매매기법", correct)
    except Exception:
        pass
    return result


def sell_ky(code: str, qty: int, reason: str = "") -> str:
    """KY 실전계좌 매도 래퍼. qty=None 이면 전량."""
    try:
        mt_ky  = _get_auto_mt_ky()
        res_ky = mt_ky.sell(code, qty)
        logger.info("[KY] SELL %s qty=%s %s → %s", code, qty, reason, res_ky[:60])
        if '✅' in res_ky:
            _tg_notify_ky(f"📉 [KY 자동매도]\n{res_ky}")
        time_str = datetime.datetime.now(pytz.timezone("Asia/Seoul")).strftime("%H:%M:%S")
        config._daily_trade_log_ky.append(f"{time_str} 📉 매도 {code} → {res_ky[:50]}")
        return res_ky
    except Exception:
        logger.exception("[KY] SELL 실패: %s", code)
        return f"❌ KY 매도 오류: {code}"


def buy_mock(code: str, amount: int, sig: dict = None) -> str:
    """트레이너 계좌 매수 래퍼. amount = 매수금액(원)."""
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


def buy_ky(code: str, amount: int, sig: dict = None) -> str:
    """KY 실전계좌 매수 래퍼. amount = 매수금액(원)."""
    kwargs = {}
    if sig:
        kwargs["buy_signals"] = sig.get("buy_count")
        kwargs["rsi"]         = sig.get("rsi")
        kwargs["macd_hist"]   = sig.get("macd_hist")
    try:
        mt_ky  = _get_auto_mt_ky()
        res_ky = mt_ky.buy(code, amount, **kwargs)
        logger.info("[KY] BUY  %s %d원 → %s", code, amount, res_ky[:60])
        if '✅' in res_ky:
            _tg_notify_ky(f"📈 [KY 자동매수]\n{res_ky}")
        time_str = datetime.datetime.now(pytz.timezone("Asia/Seoul")).strftime("%H:%M:%S")
        config._daily_trade_log_ky.append(f"{time_str} 📈 매수 {code} {amount:,}원 → {res_ky[:50]}")
        return res_ky
    except Exception:
        logger.exception("[KY] BUY 실패: %s", code)
        return f"❌ KY 매수 오류: {code}"


def smart_buy_amount(code: str) -> int:
    """
    보유 예수금 ÷ 남은 슬롯 수 → 슬롯당 배정금액.
    1순위: portfolio.db account 테이블 (항상 최신)
    2순위: KIS API (DB 실패 시)
    전일 대비 5% 이상 감소 시 → 70% 보수적 운영
    최소 50,000원 / 최대 5,000,000원 클램프.
    """
    import sqlite3 as _sqlite3
    from mock_trading.kis_client import get_current_price as get_price
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


# ── 계좌 레지스트리 + 공통 매수/매도 헬퍼 ───────────────────────────────────

# 계좌 설정 목록 — 새 계좌 추가 시 여기에만 추가하면 됨
# 반드시 함수 정의 후에 위치 (Python 모듈 로드 순서)
_ACCOUNTS = [
    {
        "id":               "trainer",
        "label":            "🔵 트레이너",
        "db_path":          os.path.join(os.path.dirname(__file__), "mock_trading", "portfolio.db"),
        "get_mt":           _get_auto_mt,
        "notify":           _tg_notify,
        "log_attr":         "_daily_trade_log",
        "last_trades_attr": "_auto_last_trades",
        "max_slots":        7,
    },
    {
        "id":               "ky",
        "label":            "🟡 KY",
        "db_path":          _KY_DB_PATH,
        "get_mt":           _get_auto_mt_ky,
        "notify":           _tg_notify_ky,
        "log_attr":         "_daily_trade_log_ky",
        "last_trades_attr": "_auto_last_trades_ky",
        "max_slots":        7,
    },
]


def _sell_for_account(acc: dict, code: str, qty, reason: str = "") -> str:
    """계좌별 공통 매도 로직."""
    mt = acc["get_mt"]()
    if acc["id"] == "trainer":
        result = mt.sell(code, qty, oracle_pool=get_db_pool())
        try:
            from learn_chart_method import update_method_trust
            correct = "익절" in reason or "목표" in reason
            update_method_trust("종합_매매기법", correct)
        except Exception:
            pass
    else:
        result = mt.sell(code, qty)
        acc["notify"](f"📉 [{acc['label']} 자동매도]\n{result}")

    label    = acc["label"]
    time_str = datetime.datetime.now(pytz.timezone("Asia/Seoul")).strftime("%H:%M:%S")
    logger.info("[%s] SELL %s qty=%s %s → %s", label, code, qty, reason, result[:60])
    getattr(config, acc["log_attr"]).append(f"{time_str} 📉 매도 {code} → {result[:50]}")
    return result


def _buy_for_account(acc: dict, code: str, amount: int, sig: dict = None) -> str:
    """계좌별 공통 매수 로직."""
    mt     = acc["get_mt"]()
    kwargs = {}
    if sig:
        kwargs["buy_signals"] = sig.get("buy_count")
        kwargs["rsi"]         = sig.get("rsi")
        kwargs["macd_hist"]   = sig.get("macd_hist")

    # 호가창 조회 → 스마트 주문 (지정가 vs 시장가)
    limit_price = 0
    try:
        from mock_trading.kis_client import get_orderbook
        ob = get_orderbook(code)
        if ob:
            ask1  = ob["ask_price"][0]  # 1호가 매도가
            spread_pct = (ask1 - ob["bid_price"][0]) / ask1 * 100 if ask1 else 0
            bid_total  = ob["bid_total"]
            ask_total  = ob["ask_total"]
            if ask_total > 0 and (bid_total > ask_total * 1.5 or spread_pct > 0.5):
                limit_price = ask1
                logger.info("호가 지정가 매수 %s: 1호가=%d 스프레드=%.2f%% 매수잔량비=%.1f",
                            code, ask1, spread_pct, bid_total / max(ask_total, 1))
    except Exception:
        pass

    if acc["id"] == "trainer":
        result = mt.buy(code, amount, oracle_pool=get_db_pool(), limit_price=limit_price, **kwargs)
    else:
        result = mt.buy(code, amount, limit_price=limit_price, **kwargs)
        if "✅" in result:
            acc["notify"](f"📈 [{acc['label']} 자동매수]\n{result}")

    label    = acc["label"]
    time_str = datetime.datetime.now(pytz.timezone("Asia/Seoul")).strftime("%H:%M:%S")
    logger.info("[%s] BUY  %s %d원 → %s", label, code, amount, result[:60])
    getattr(config, acc["log_attr"]).append(f"{time_str} 📈 매수 {code} {amount:,}원 → {result[:50]}")
    return result


def _smart_buy_amount_for_account(acc: dict, code: str) -> int:
    """계좌별 공통 매수 금액 계산."""
    import sqlite3 as _sqlite3
    from mock_trading.kis_client import get_current_price as get_price
    MAX_SLOTS  = acc.get("max_slots", 7)
    db_path    = acc["db_path"]
    cash       = 0
    conservative = False

    try:
        con      = _sqlite3.connect(db_path)
        row      = con.execute("SELECT value FROM account WHERE key='cash'").fetchone()
        prev_row = con.execute("SELECT value FROM account WHERE key='prev_day_cash'").fetchone()
        con.close()
        if row:
            cash = float(row[0])
        if prev_row and cash > 0:
            prev_cash = float(prev_row[0])
            if prev_cash > 0 and cash < prev_cash * 0.95:
                conservative = True
    except Exception:
        logger.warning("[%s] _smart_buy_amount_for_account: DB 읽기 실패", acc["label"])

    if cash <= 0:
        try:
            if acc["id"] == "trainer":
                from mock_trading.kis_client import get_balance
            else:
                from mock_trading.kis_client_ky import get_balance
            bal  = get_balance()
            cash = bal.get("cash", 0)
        except Exception:
            pass

    remain = 1
    try:
        used   = len(acc["get_mt"]()._get_holdings())
        remain = max(1, MAX_SLOTS - used)
    except Exception:
        pass

    if cash <= 0:
        cash = 1_000_000

    effective_cash = cash * 0.7 if conservative else cash
    amount = int(effective_cash / remain)
    amount = max(50_000, min(5_000_000, amount))

    price = get_price(code) or 0
    if price > 0 and amount < price:
        amount = price

    # KY 계좌: KIS API 실제 주문가능금액으로 상한 적용
    if acc["id"] == "ky":
        try:
            from mock_trading.kis_client_ky import get_available_amount
            avail = get_available_amount(code, price)
            if avail == 0:
                logger.info("[%s] %s 주문가능금액 0원 → 매수 스킵", acc["label"], code)
                return 0
            if avail > 0 and amount > avail:
                logger.warning("[%s] 주문가능금액 초과 조정: %d → %d원", acc["label"], amount, avail)
                amount = avail
        except Exception:
            pass

    logger.info("[%s] 매수금액 %s: 예수금%d%s ÷ 슬롯%d = %d원",
                acc["label"], code, int(cash), "(보수적)" if conservative else "", remain, amount)
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
            ts = (r[4] or "")[:10]
            rows.append(f"  [{ts}] {r[0]} RSI={r[1]} 신호={r[2]}/16 → 손익 {r[3]:+.1f}%")

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



# 종목코드 → 업종 역방향 맵 (train_sector_kis.py SECTOR_STOCKS 기반)
_CODE_TO_SECTOR = {
    # 반도체
    "005930": "반도체", "000660": "반도체", "042700": "반도체",
    "336370": "반도체", "240810": "반도체",
    # 방산
    "012450": "방산",   "079550": "방산",   "064350": "방산",   "047050": "방산",
    # 자동차
    "005380": "자동차", "000270": "자동차", "012330": "자동차",
    "011210": "자동차", "161390": "자동차",
    # 2차전지
    "373220": "2차전지","006400": "2차전지","247540": "2차전지",
    "086520": "2차전지","096530": "2차전지","278280": "2차전지",
    # 바이오
    "207940": "바이오", "068270": "바이오", "128940": "바이오",
    "196170": "바이오", "028300": "바이오", "141080": "바이오",
    # IT플랫폼
    "035420": "IT플랫폼","035720": "IT플랫폼","018260": "IT플랫폼",
    "259960": "IT플랫폼","036570": "IT플랫폼",
    # 금융
    "105560": "금융",   "055550": "금융",   "086790": "금융",
    "316140": "금융",   "032830": "금융",   "000810": "금융",
    # 철강/소재
    "005490": "철강/소재","004020": "철강/소재","010060": "철강/소재",
    "011000": "철강/소재","002380": "철강/소재",
    # 건설
    "000720": "건설",   "047040": "건설",   "006360": "건설",
    "000080": "건설",   "008770": "건설",
    # 에너지
    "096770": "에너지", "010950": "에너지", "015760": "에너지", "267250": "에너지",
    # 조선/해운
    "009540": "조선/해운","042660": "조선/해운","010140": "조선/해운",
    "011200": "조선/해운","000120": "조선/해운",
    # 반도체장비/소재
    "403870": "반도체장비/소재","079940": "반도체장비/소재","357780": "반도체장비/소재",
    "285130": "반도체장비/소재","166090": "반도체장비/소재",
    # 게임/엔터
    "041510": "게임/엔터","035900": "게임/엔터","352820": "게임/엔터",
    # 유통/소비
    "139480": "유통/소비","004170": "유통/소비","069960": "유통/소비",
    "007310": "유통/소비","271560": "유통/소비",
    # 통신
    "017670": "통신",   "030200": "통신",   "032640": "통신",
    # ETF
    "069500": "ETF", "122630": "ETF", "229200": "ETF",
    "396500": "ETF", "494310": "ETF", "462330": "ETF", "267270": "ETF",
}


def _ollama_buy_decision(code: str, name: str, sig: dict) -> dict:
    """
    PC Ollama(mistral)에게 매수 여부 + 전략 유형 판단 요청.
    반환: {"action": "BUY"|"SKIP", "trade_type": "단타"|"스윙", "reason": str}
    실패 시 폴백: buy_count >= 6
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

    # 업종별 학습 기법 RAG 조회 (chart_method_memory) — 섹터 필터 우선
    sector = _CODE_TO_SECTOR.get(code, "전체")
    rag_method = ""
    try:
        from rag_store import search_chart_method
        rag_method = search_chart_method(
            f"상승진입기법 하락경계 타임프레임신뢰도", n_results=3, sector=sector
        )
    except Exception as _me:
        logger.debug("업종 RAG 조회 실패: %s", _me)

    # 업종 파라미터 로드 (없거나 오래됐으면 백그라운드 자동 학습 트리거)
    _sp = {}
    try:
        import sector_params as _sp_mod
        _sp = _sp_mod.get(sector)
    except Exception as _spe:
        logger.debug("sector_params 조회 실패: %s", _spe)

    prompt = (
        f"종목: {name}({code}) | 업종: {sector} | 총신호={sig['buy_count']}/12\n\n"

        "월봉 (장기 추세)\n"
        f"  기준선{'위(✅)' if s.get('월봉_일목균형표') else '아래(❌)'} | ADX={v('월봉_ADX')} | RSI={v('월봉_RSI')} | MACD={v('월봉_MACD')}\n\n"

        "주봉 (스윙 방향)\n"
        f"  기준선{'위(✅)' if s.get('주봉_일목균형표') else '아래(❌)'} | ADX={v('주봉_ADX')} | RSI={v('주봉_RSI')} | MACD={v('주봉_MACD')}\n\n"

        "일봉 (진입 판단)\n"
        f"  기준선{'위(✅)' if s.get('일봉_일목균형표') else '아래(❌)'} | ADX={v('일봉_ADX')} | RSI={v('일봉_RSI')} | MACD={v('일봉_MACD')}\n"
        f"  MA5={ma5:,.0f} MA20={ma20:,.0f} MA60={ma60:,.0f} MA120={ma120:,.0f}\n"
        f"  정배열={정배열} | 현재가>MA20={가격위치} | MACD히스트={sig['macd_hist']} | ADX={sig['adx']}\n\n"

        "분봉 타이밍 (15/30/60분봉 — 타이밍 참고, BUY/SKIP 판단 영향 없음)\n"
        f"  기준선{v('분봉_일목균형표')} | ADX={v('분봉_ADX')} | MACD={v('분봉_MACD')}\n\n"

        "단타 (3분봉 — 3/4 이상이면 당일청산)\n"
        f"  기준선{v('분봉_3분_일목균형표')} | ADX={v('분봉_3분_ADX')} | MACD={v('분봉_3분_MACD')}\n\n"
    )

    if _sp and _sp.get("note") and _sp.get("note") != "기본값":
        prompt += (
            f"## {sector} 업종 최적 파라미터 (10년 데이터 도출)\n"
            f"  권장최소신호: {_sp.get('min_signal',6)}/12 | "
            f"외국인기관동시: {'필수' if _sp.get('require_both') else '불필요'} | "
            f"권장보유: {_sp.get('hold_days',10)}일\n"
            f"  근거: {_sp.get('note','')}\n\n"
        )

    if rag_method:
        prompt += f"## {sector} 업종 학습 기법 (10년 데이터 기반 — 참고용)\n{rag_method}\n\n"

    rag = _rag_trade_history(code, sig)
    if rag:
        prompt += rag + "\n\n"

    trade_type = _classify_trade_type(sig)  # 3분봉 기반 코드 결정 (단타/스윙)
    prompt += (
        "위 신호와 업종 학습 기법을 종합해서 매수 여부를 판단하세요.\n"
        "과거 패턴은 참고용이며, 현재 신호가 최우선입니다.\n"
        "JSON만 반환:\n"
        '{"action":"BUY"|"SKIP","reason":"한줄설명"}'
    )
    try:
        resp = call_mistral_only(
            prompt,
            system="당신은 주식 매매 판단 AI입니다. JSON만 반환하고 다른 텍스트는 쓰지 마세요.",
            use_tools=False,
        )
        m = _re.search(r'\{[^{}]*"action"[^{}]*\}', resp, _re.DOTALL)
        if m:
            d = _json.loads(m.group())
            if d.get("action") in ("BUY", "SKIP"):
                d["trade_type"] = trade_type
                logger.info("Ollama 매수판단 %s: %s [%s] — %s",
                            code, d["action"], d["trade_type"], d.get("reason",""))
                return d
    except Exception:
        logger.warning("Ollama 매수판단 실패 %s, 폴백룰 적용", code)
    return {"action": "SKIP", "trade_type": trade_type, "reason": f"폴백SKIP(신호 {sig['buy_count']}/12, Ollama실패)"}


def select_volume_smart_chart() -> list:
    """
    차트 신호 기반 후보 선발 — DB 3개월 워치리스트 전체를 병렬 스캔,
    신호수(buy_count) 내림차순 정렬 후 BUY(≥6) 상위 7개 반환.
    거래량은 참고용 로그만 출력.
    [(code, sig_dict), ...] 최대 7개 반환.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    watchlist = get_watchlist_from_db(months=3)
    watch_codes = {code for code, _, __, ___ in watchlist}

    # 거래량 TOP20 — 참고용 로그만
    vol_top = get_volume_surge_top20()
    vol_set = set(vol_top)
    overlap = [c for c in watch_codes if c in vol_set]
    logger.info("워치리스트%d종목 병렬 스캔 시작 (거래량TOP20 참고: %d종목 겹침)",
                len(watch_codes), len(overlap))

    def _scan_one(code):
        sig = calculate_chart_signals(code, scan_mode=True)
        if not sig:
            return None
        name = _get_name_by_code(code)
        vol_tag = " [거래량상위]" if code in vol_set else ""
        sig["name"] = name
        sig["vol_tag"] = vol_tag
        sig["sector"] = _CODE_TO_SECTOR.get(code, "전체")  # PC LLM 전략 검증용
        return (code, sig)

    results = []
    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = {ex.submit(_scan_one, code): code for code in watch_codes}
        for fut in as_completed(futures):
            try:
                r = fut.result()
                if r:
                    results.append(r)
            except Exception:
                pass

    # 신호수 내림차순 정렬 → 업종별 min_signal 필터 (없으면 기본 6)
    results.sort(key=lambda x: x[1].get("buy_count", 0), reverse=True)
    try:
        import sector_params as _sp_mod
        def _min_sig(code):
            s = _CODE_TO_SECTOR.get(code, "전체")
            return _sp_mod.get(s).get("min_signal", 6)
    except Exception:
        def _min_sig(code): return 6
    buy_candidates = [(code, sig) for code, sig in results
                      if sig.get("buy_count", 0) >= _min_sig(code)]

    logger.info("차트 BUY 후보: %d종목 (신호≥6/12)", len(buy_candidates))
    logger.info("[REPORT] 스캔BUY후보:%d top:%s", len(buy_candidates), [sig.get("name", c) for c, sig in buy_candidates[:5]])
    for code, sig in buy_candidates[:10]:
        logger.info("  %s(%s) %d/12%s", sig.get("name", code), code,
                    sig.get("buy_count", 0), sig.get("vol_tag", ""))

    targets = []
    for code, sig in buy_candidates:
        decision = _ollama_buy_decision(code, sig.get("name", code), sig)
        if decision["action"] == "BUY":
            sig["trade_type"] = decision.get("trade_type", "스윙")
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


def _log_pc_learning_data(code: str, name: str, prev_count: int, new_count: int, min_signal: int, signals: dict = None):
    """PC LLM의 분석 결과를 학습 이력 파일에 저장 (신호 조합 + 보조 지표 강도 포함)."""
    try:
        _BASE_DIR = os.path.dirname(os.path.abspath(__file__))
        learning_path = os.path.join(_BASE_DIR, "pc_learning_history.json")

        # 기존 데이터 로드
        learning_data = []
        if os.path.exists(learning_path):
            try:
                with open(learning_path, 'r', encoding='utf-8') as f:
                    learning_data = json.load(f)
                    if not isinstance(learning_data, list):
                        learning_data = []
            except Exception:
                learning_data = []

        # 신호 조합 계산 (월/주/일봉 강도)
        signals = signals or {}
        m_ichimoku = signals.get('월봉_일목균형표', False)
        w_ichimoku = signals.get('주봉_일목균형표', False)
        d_ichimoku = signals.get('일봉_일목균형표', False)

        m_strength = sum([
            signals.get('월봉_일목균형표', False),
            signals.get('월봉_ADX', False),
            signals.get('월봉_RSI', False),
            signals.get('월봉_MACD', False)
        ])
        w_strength = sum([
            signals.get('주봉_일목균형표', False),
            signals.get('주봉_ADX', False),
            signals.get('주봉_RSI', False),
            signals.get('주봉_MACD', False)
        ])
        d_strength = sum([
            signals.get('일봉_일목균형표', False),
            signals.get('일봉_ADX', False),
            signals.get('일봉_RSI', False),
            signals.get('일봉_MACD', False)
        ])

        def classify_strength(count):
            return "strong" if count >= 3 else "weak"

        signal_combo = f"{classify_strength(m_strength)}/{classify_strength(w_strength)}/{classify_strength(d_strength)}"

        # 보조 지표 강도 평가 (타임프레임별)
        auxiliary_strengths = {
            "월": evaluate_auxiliary_strength("월봉",
                                              signals.get("월봉_adx_val"),
                                              signals.get("월봉_rsi_val"),
                                              signals.get("월봉_macd_val")),
            "주": evaluate_auxiliary_strength("주봉",
                                              signals.get("주봉_adx_val"),
                                              signals.get("주봉_rsi_val"),
                                              signals.get("주봉_macd_val")),
            "일": evaluate_auxiliary_strength("일봉",
                                              signals.get("일봉_adx_val"),
                                              signals.get("일봉_rsi_val"),
                                              signals.get("일봉_macd_val"))
        }

        # 종합 신뢰도 계산 (메인 일목 + 보조 지표)
        ichimoku_present = m_ichimoku or w_ichimoku or d_ichimoku
        reliability_score = calculate_composite_reliability(ichimoku_present, auxiliary_strengths)

        # 신규 항목 추가
        today = datetime.datetime.now(pytz.timezone("Asia/Seoul")).date().isoformat()
        entry = {
            "code": code,
            "name": name,
            "date": today,
            "signal_shift": f"{prev_count}→{new_count} ({'+' if new_count > prev_count else ''}{new_count - prev_count})",
            "pc_min_signal_suggestion": min_signal,
            "signal_combo": signal_combo,  # 신호 조합 (예: strong/strong/weak)
            "signal_strengths": {  # 간단한 신호 강도
                "monthly": m_strength,
                "weekly": w_strength,
                "daily": d_strength
            },
            "auxiliary_strengths": {  # 보조 지표 상세 강도
                "월": {
                    "adx": auxiliary_strengths["월"].get("adx_strength", 0),
                    "rsi": auxiliary_strengths["월"].get("rsi_strength", 0),
                    "macd": auxiliary_strengths["월"].get("macd_strength", 0),
                    "total": auxiliary_strengths["월"].get("total_strength", 0)
                },
                "주": {
                    "adx": auxiliary_strengths["주"].get("adx_strength", 0),
                    "rsi": auxiliary_strengths["주"].get("rsi_strength", 0),
                    "macd": auxiliary_strengths["주"].get("macd_strength", 0),
                    "total": auxiliary_strengths["주"].get("total_strength", 0)
                },
                "일": {
                    "adx": auxiliary_strengths["일"].get("adx_strength", 0),
                    "rsi": auxiliary_strengths["일"].get("rsi_strength", 0),
                    "macd": auxiliary_strengths["일"].get("macd_strength", 0),
                    "total": auxiliary_strengths["일"].get("total_strength", 0)
                }
            },
            "reliability_score": reliability_score,  # 종합 신뢰도 (0-100)
            "timestamp": datetime.datetime.now(pytz.timezone("Asia/Seoul")).isoformat()
        }

        # 파일 append + 저장 — 복수 _call_pc_async 스레드 동시 쓰기 방지
        with _learning_data_lock:
            # 락 안에서 최신 파일 다시 읽기 (다른 스레드가 이미 썼을 수 있음)
            if os.path.exists(learning_path):
                try:
                    with open(learning_path, 'r', encoding='utf-8') as f:
                        fresh = json.load(f)
                        if isinstance(fresh, list):
                            learning_data = fresh
                except Exception:
                    pass
            learning_data.append(entry)
            learning_data = learning_data[-500:]
            with open(learning_path, 'w', encoding='utf-8') as f:
                json.dump(learning_data, f, ensure_ascii=False, indent=2)

    except Exception as e:
        logger.warning("학습데이터 저장 실패: %s", e)


def _get_pc_cooldown_min() -> int:
    """장 시간대별 쿨다운 반환 (분).
    장 초반(08:30~09:15): 10분 — 변동성 빠르게 포착
    오전장(09:15~11:30): 15분
    이후(11:30~20:00):   30분
    """
    kst = datetime.datetime.now(pytz.timezone("Asia/Seoul"))
    t = kst.time()
    if datetime.time(8, 30) <= t < datetime.time(9, 15):
        return 10
    if datetime.time(9, 15) <= t < datetime.time(11, 30):
        return 15
    return 30


def _monitor_signal_shifts():
    """
    보유 종목의 신호 변화 감지 — PC LLM이 학습데이터로 사용.
    개선사항:
      1. 가변 쿨다운: 장 초반 10분 / 오전장 15분 / 이후 30분
      2. 날짜 기반 일일 호출 수 자동 초기화
      3. 쿨다운 dict 만료 항목 자동 정리 (메모리 누수 방지)
      4. 복합 필터: 신호 변화 + 거래량 동반 여부
    """
    global _pc_call_cooldown, _pc_daily_calls, _pc_daily_calls_date

    if not is_trading_hours():
        return

    # ── 날짜 기반 일일 호출 수 초기화 (락 적용) ────────────────────────────
    kst_now = datetime.datetime.now(pytz.timezone("Asia/Seoul"))
    today_str = kst_now.date().isoformat()
    with config._auto_lock:
        if _pc_daily_calls_date != today_str:
            _pc_daily_calls = 0
            _pc_daily_calls_date = today_str
            logger.debug("PC 일일 호출 카운터 초기화 (%s)", today_str)
        if _pc_daily_calls >= PC_MAX_DAILY_CALLS:
            logger.debug("PC 일일 최대 호출 도달 (%d/%d)", _pc_daily_calls, PC_MAX_DAILY_CALLS)
            return

    # ── 만료된 쿨다운 항목 정리 (락 적용, max 60분) ─────────────────────────
    with config._auto_lock:
        cutoff = kst_now - datetime.timedelta(minutes=65)  # 최대 쿨다운 60분 + 여유
        expired_codes = [c for c, t in _pc_call_cooldown.items() if t < cutoff]
        for c in expired_codes:
            del _pc_call_cooldown[c]

    cooldown_min = _get_pc_cooldown_min()

    try:
        for code, name, qty, avg_price in _get_auto_mt()._get_holdings():
            if _pc_daily_calls >= PC_MAX_DAILY_CALLS:
                break

            # ── 쿨다운 체크 + pending_buys 읽기 (단일 락으로 TOCTOU 방지) ──
            with config._auto_lock:
                last_call = _pc_call_cooldown.get(code)
                if last_call:
                    elapsed_min = (kst_now - last_call).total_seconds() / 60
                    if elapsed_min < cooldown_min:
                        logger.debug("PC 쿨다운 중: %s (%.1f분 / %d분)", name, elapsed_min, cooldown_min)
                        continue
                prev = config._pending_buys.get(code, {})

            if not prev:
                continue

            sig = calculate_chart_signals(code, scan_mode=True)
            if not sig:
                continue

            prev_count = prev.get("buy_count", 0)
            new_count = sig.get("buy_count", 0)
            delta = new_count - prev_count

            # ── 기본 임계값 필터 ───────────────────────────────────────────
            if abs(delta) < PC_SIGNAL_THRESHOLD:
                continue

            # ── 복합 필터: 거래량 동반 여부 ────────────────────────────────
            # 신호가 하락 방향(-2 이하)이면 거래량 없어도 경보 가치 있음
            # 신호가 상승 방향(+2 이상)이면 거래량 동반 시에만 PC 호출 (노이즈 감소)
            volume_ratio = sig.get("volume_ratio", 1.0)  # 현재거래량 / 평균거래량
            if delta > 0 and volume_ratio < 0.8:
                logger.debug("📊 신호↑ but 거래량 미동반 (ratio=%.2f) — PC 호출 스킵: %s", volume_ratio, name)
                continue

            logger.info("🔍 신호 변화 감지: %s(%s) %d→%d (거래량비율=%.2f)",
                        name, code, prev_count, new_count, volume_ratio)

            # ── 캐시 업데이트 ──────────────────────────────────────────────
            with config._auto_lock:
                if code in config._pending_buys:
                    config._pending_buys[code]["buy_count"] = new_count

            signals = sig.get('signals', {})

            # ── 쿨다운 기록 & 일일 카운터 증가 (락 적용) ──────────────────
            with config._auto_lock:
                _pc_call_cooldown[code] = kst_now
                _pc_daily_calls += 1

            if pc_director:
                def _call_pc_async(c=code, n=name, pc=prev_count, nc=new_count, s=signals):
                    try:
                        min_signal = pc_director.analyze_signal_shift(c, n, pc, nc, s)
                        if min_signal is not None:
                            logger.info("💡 PC제안: %s min_signal=%d → 학습데이터 축적", n, min_signal)
                            _log_pc_learning_data(c, n, pc, nc, min_signal, s)
                    except Exception as e:
                        logger.debug("PC 분석 실패: %s", e)

                threading.Thread(target=_call_pc_async, daemon=True).start()
            else:
                def _log_async(c=code, n=name, pc=prev_count, nc=new_count, s=signals):
                    _log_pc_learning_data(c, n, pc, nc, 0, s)

                threading.Thread(target=_log_async, daemon=True).start()

    except Exception:
        logger.debug("신호 변화 감지 실패")


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
            # 신호 스냅샷 갱신 (락으로 보호)
            with config._auto_lock:
                if code in config._pending_buys:
                    config._pending_buys[code]["signals"] = new_sig.get("signals", {})
                    config._pending_buys[code]["macd_hist"] = new_sig.get("macd_hist", 0)
            if decision["action"] == "SKIP":
                expired.append(code)  # SKIP 전환 시 감시 종료
        except Exception:
            logger.exception("_check_pending_buys 오류: %s", code)
    with config._auto_lock:
        for code in expired:
            config._pending_buys.pop(code, None)


# ── Ollama 매도 판단 ─────────────────────────────────────────────────────────

def _ollama_sell_decision(code: str, name: str, pnl: float, qty: int,
                          avg_price: float, current: float,
                          trade_type: str = "스윙",
                          last_trades: dict = None) -> dict:
    """
    Ollama에게 매도 여부 + 다음 확인 시각 판단 요청.
    trade_type: "단타"(당일청산, 익절+2%/손절-1%) | "스윙"(익절+5%/손절-2%)
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

    if last_trades is None:
        last_trades = config._auto_last_trades

    hold_days = 0
    try:
        d = last_trades.get(code, {}).get("date")
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
                   f"매수신호={sig['buy_count']}/12")

    rag_sell = _rag_trade_history(code, sig or {})

    # 업종별 학습 기법 조회 (chart_method_memory) — 섹터 필터 우선
    sell_sector = _CODE_TO_SECTOR.get(code, "전체")
    rag_method_sell = ""
    try:
        from rag_store import search_chart_method
        rag_method_sell = search_chart_method(
            f"하락경계 매도 익절 손절 타이밍 {trade_type}", n_results=2, sector=sell_sector)
    except Exception:
        pass

    kst_time_str = datetime.datetime.now(pytz.timezone("Asia/Seoul")).strftime("%H:%M")
    prompt = (
        f"종목: {name}({code})\n"
        f"현재가: {int(current):,}원 | 평균단가: {int(avg_price):,}원 | 손익: {pnl:+.2f}%\n"
        f"현재시각: {kst_time_str} KST\n"
        f"오늘 변동폭: {day_range_pct:.1f}% | ATR5: {atr5_pct:.1f}%\n"
        f"차트: {sig_txt}\n"
        f"보유일수: {hold_days}일\n"
        + (f"[학습된 매도 기법]\n{rag_method_sell}\n" if rag_method_sell else "")
        + (rag_sell + "\n" if rag_sell else "")
        + f"\n전략: {trade_type}\n"
        + ("\n판단 기준 (단타 — 당일 청산 목표):\n"
           "- 익절 목표 +2%, 손절 -1%\n"
           "- HOLD 시 check_after는 최대 5분\n"
           "- 추세 조금이라도 꺾이면 빠르게 SELL\n"
           "\n"
           if trade_type == "단타" else
           "\n판단 기준 (스윙 — 수일 보유 가능):\n"
           "- 익절 목표 +5%, 손절 -2%\n"
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
        if pnl <= -2:  # -3% → -2% 강화
            return {"action": "SELL_ALL",     "ratio": 1.0, "check_after": 0,  "reason": "폴백 스윙 -2% 손절"}
        return     {"action": "HOLD",         "ratio": 0.0, "check_after": 15, "reason": "폴백 스윙 유지"}


# ── 핵심 매매 사이클 ─────────────────────────────────────────────────────────

def auto_trade_cycle():
    """
    30초마다 실행. 등록된 모든 계좌를 독립적으로 처리.
    1. 장외시간 즉시 리턴
    2. 각 계좌 보유종목 → Ollama 매도판단 (변동성+차트+PnL 종합)
    3. 각 계좌 신규매수: 거래량∩순매수→차트 ≥6/12 상위 7종목 × 계좌별 예수금비례
    """
    if not config._auto_enabled or not is_trading_hours():
        return

    # 신호 변화 감지 (PC LLM이 학습데이터로 분석)
    _monitor_signal_shifts()

    # BUY 결정 후 신호 변화 즉시 감시
    _check_pending_buys()

    from mock_trading.kis_client import get_current_price as get_price, REAL_TRADE, get_balance
    kst_now  = datetime.datetime.now(pytz.timezone("Asia/Seoul"))
    time_str = kst_now.strftime("%H:%M:%S")
    today    = kst_now.date().isoformat()
    all_bought = []

    # ── 신규매수 후보 (공통 1회 조회 — 비싼 연산) ────────────────────
    # 09:00~09:10 KIS API 불안정 구간 — 신규매수만 차단 (매도/HOLD는 정상)
    can_buy = not (kst_now.hour == 9 and kst_now.minute < 10)
    new_targets = []
    if can_buy:
        try:
            new_targets = select_volume_smart_chart()
        except Exception:
            logger.exception("신규 후보 탐색 실패")

    # ── 계좌별 루프 ──────────────────────────────────────────────────
    for acc in _ACCOUNTS:
        last_trades = getattr(config, acc["last_trades_attr"])
        trade_log   = getattr(config, acc["log_attr"])

        # ── 1. 보유종목 관리 ─────────────────────────────────────────
        holdings = []
        try:
            holdings = acc["get_mt"]()._get_holdings()

            # 트레이너 실전 매매 시 KIS API 평균단가 덮어쓰기
            if acc["id"] == "trainer" and REAL_TRADE:
                try:
                    kis_bal = get_balance()
                    kis_avg = {h["code"]: h["avg_price"] for h in kis_bal.get("holdings", [])}
                    holdings = [
                        (code, name, qty, kis_avg.get(code, avg_price))
                        for code, name, qty, avg_price in holdings
                    ]
                except Exception as _be:
                    logger.warning("[%s] KIS 평균단가 조회 실패: %s", acc["label"], _be)

            for code, name, qty, avg_price in holdings:
                try:
                    current = get_price(code)
                    if not current:
                        continue
                    pnl = (current - avg_price) / avg_price * 100

                    last       = last_trades.get(code, {})
                    next_check = last.get("next_check")
                    if next_check and kst_now < next_check:
                        remain = int((next_check - kst_now).total_seconds() / 60)
                        logger.debug("⏭ [%s] %s(%s) 스킵 — 다음확인 %d분 후",
                                     acc["label"], name, code, remain)
                        continue

                    trade_type = last_trades.get(code, {}).get("trade_type", "스윙")

                    # 단타 15:10 강제 청산 (평일만)
                    if (trade_type == "단타" and kst_now.weekday() < 5
                            and kst_now.hour == 15 and kst_now.minute >= 10):
                        result = _sell_for_account(acc, code, None, reason="단타 장마감 강제청산")
                        logger.info("🔔 [%s] 단타 강제청산 %s(%s): %+.1f%%",
                                    acc["label"], name, code, pnl)
                        trade_log.append(
                            f"{time_str} 🔔 단타강제청산 {name}({code}) {pnl:+.1f}%")
                        last_trades[code] = {"time": time_str, "action": "SELL_ALL", "pnl": pnl}
                        continue

                    decision    = _ollama_sell_decision(code, name, pnl, qty, avg_price, current,
                                                        trade_type=trade_type,
                                                        last_trades=last_trades)
                    action      = decision["action"]
                    ratio       = decision.get("ratio", 0.3)
                    reason      = decision.get("reason", "")
                    check_after = decision.get("check_after", 15)
                    next_dt     = kst_now + datetime.timedelta(minutes=check_after)

                    if action == "SELL_PARTIAL":
                        sell_qty = max(1, int(qty * ratio))
                        # 남은 수량이 1주 이하면 전량 매도 (수수료 낭비 방지)
                        if sell_qty >= qty or (qty - sell_qty) <= 1:
                            action   = "SELL_ALL"
                            sell_qty = qty
                        result = _sell_for_account(
                            acc, code,
                            sell_qty if action == "SELL_PARTIAL" else None,
                            reason=f"Ollama: {reason}"
                        )
                        if action == "SELL_ALL":
                            emoji = "🔴" if pnl < 0 else "💰"
                            logger.info("%s [%s] 전량매도(부분→전량) %s(%s): %+.1f%%",
                                        emoji, acc["label"], name, code, pnl)
                            trade_log.append(
                                f"{time_str} {emoji} 전량매도 {name}({code}) {pnl:+.1f}%\n"
                                f"  └ {reason}")
                            last_trades[code] = {"time": time_str, "action": "SELL_ALL",
                                                 "pnl": pnl, "next_check": None}
                        else:
                            emoji = "🤑" if pnl >= 0 else "🟡"
                            logger.info("%s [%s] 부분매도 %s(%s): %+.1f%% %d주",
                                        emoji, acc["label"], name, code, pnl, sell_qty)
                            trade_log.append(
                                f"{time_str} {emoji} 부분매도 {name}({code}) {pnl:+.1f}% {sell_qty}주\n"
                                f"  └ {reason}")
                            last_trades[code] = {"time": time_str, "action": "SELL_PARTIAL",
                                                 "pnl": pnl, "next_check": next_dt}

                    elif action == "SELL_ALL":
                        result = _sell_for_account(acc, code, None, reason=f"Ollama: {reason}")
                        emoji  = "🔴" if pnl < 0 else "💰"
                        logger.info("%s [%s] 전량매도 %s(%s): %+.1f%%",
                                    emoji, acc["label"], name, code, pnl)
                        trade_log.append(
                            f"{time_str} {emoji} 전량매도 {name}({code}) {pnl:+.1f}%\n  └ {reason}")
                        last_trades[code] = {"time": time_str, "action": "SELL_ALL",
                                             "pnl": pnl, "next_check": None}

                    else:  # HOLD
                        logger.info("⏸ [%s] HOLD %s(%s): %+.1f%% → %d분 후 재확인",
                                    acc["label"], name, code, pnl, check_after)
                        last_trades[code] = {**last, "next_check": next_dt}

                except Exception:
                    logger.exception("[%s] 보유종목 처리 오류: %s", acc["label"], code)

        except Exception:
            logger.exception("[%s] holdings 조회 실패", acc["label"])

        # ── 2. 신규 매수 ─────────────────────────────────────────────
        if not can_buy:
            logger.info("⏳ [%s] 09:10 이전 신규매수 대기", acc["label"])
            continue

        if not new_targets:
            continue

        try:
            bought     = []
            held_codes = {c for c, *_ in holdings}
            for code, sig in new_targets:
                last = last_trades.get(code, {})
                if code in held_codes:
                    continue
                if last.get("date") == today and last.get("action") in ("BUY", "SELL_ALL", "SELL_PARTIAL"):
                    continue

                sector   = sig.get("sector", "")
                approved, reason = _validate_trade_with_strategy(code, sig, sector)
                if not approved:
                    logger.info("⛔ [%s] %s(%s) 거부: %s",
                                acc["label"], sig.get("name", code), code, reason)
                    continue

                try:
                    amount = _smart_buy_amount_for_account(acc, code)
                    if amount <= 0:
                        continue
                    result = _buy_for_account(acc, code, amount, sig=sig)
                    if "❌" in result:
                        logger.warning("[%s] 매수 실패 %s: %s", acc["label"], code, result[:60])
                        continue
                    trade_type = sig.get("trade_type", "스윙")
                    name       = sig.get("name", code)
                    last_trades[code] = {
                        "time": time_str, "action": "BUY", "date": today,
                        "signals": sig["buy_count"], "rsi": sig["rsi"],
                        "trade_type": trade_type,
                    }
                    # 신호 감시 등록은 트레이너만 (PC LLM 학습용)
                    if acc["id"] == "trainer":
                        config._pending_buys[code] = {
                            "name": name,
                            "signals": sig.get("signals", {}),
                            "buy_count": sig["buy_count"],
                            "macd_hist": sig["macd_hist"],
                            "time": kst_now,
                            "trade_type": trade_type,
                        }
                    bought.append(code)
                    all_bought.append(code)
                    logger.info("🚀 [%s] 신규매수 %s(%s) [%s]: %d원 [%s]",
                                acc["label"], name, code, trade_type, amount, reason)
                    trade_log.append(
                        f"{time_str} 🟢 신규매수 {name}({code}) [{trade_type}] {amount:,}원\n"
                        f"  └ RSI={sig['rsi']} MACD={sig['macd_hist']} "
                        f"ADX={sig.get('adx','?')} 신호={sig['buy_count']}/12\n"
                        f"  └ {reason}"
                    )
                except Exception:
                    logger.exception("[%s] 신규매수 오류: %s", acc["label"], code)

        except Exception:
            logger.exception("[%s] 신규매수 처리 실패", acc["label"])

    logger.info("[자동매매 %s] 신규매수:%s", time_str, all_bought or "없음")
    for _acc in _ACCOUNTS:
        _tlog = getattr(config, _acc["log_attr"], [])
        _buys = sum(1 for e in _tlog if "신규매수" in e)
        _sells = sum(1 for e in _tlog if "전량매도" in e or "부분매도" in e)
        logger.info("[REPORT] %s 누적매수:%d 누적매도:%d", _acc["label"], _buys, _sells)
    return all_bought  # auto_trade_loop에서 신규매수 여부 판단용


# ── 30초 schedule 루프 ───────────────────────────────────────────────────────

def _restore_today_trades():
    """서버 재시작 시 당일 매수 기록을 각 계좌 DB에서 복원 → 중복매수 방지."""
    import sqlite3 as _sqlite3
    today = datetime.datetime.now(pytz.timezone("Asia/Seoul")).date().isoformat()
    for acc in _ACCOUNTS:
        db_path     = acc["db_path"]
        last_trades = getattr(config, acc["last_trades_attr"])
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
                if ticker not in last_trades or db_action == "SELL_ALL":
                    last_trades[ticker] = {
                        "action": db_action, "date": today,
                        "time": created_at[11:19] if len(created_at) > 10 else "",
                        "trade_type": "스윙",
                    }
            if rows:
                logger.info("[%s] 당일 매매 복원: %d건", acc["label"], len(rows))
        except Exception:
            logger.exception("[%s] _restore_today_trades 실패", acc["label"])


def _validate_trade_with_strategy(code: str, sig: dict, sector: str = "") -> tuple[bool, str]:
    """
    PC LLM 전략(관리자)에 따라 이 매수/매도가 승인되는지 검증.

    Returns:
        (is_approved, reason)
    """
    if not pc_director:
        return True, "pc_director 미활성화"

    try:
        strategy = pc_director.get_current_strategy()
        if strategy["status"] != "ready":
            return True, f"전략 상태: {strategy['status']}"

        buy_count = sig.get("buy_count", 0)

        # 1) 위험도 기반 신호 임계값
        risk_to_min_signal = {"low": 7, "normal": 6, "high": 5}
        min_signal_required = risk_to_min_signal.get(strategy.get("risk_level", "normal"), 6)

        # 2) 업종별 오버라이드 확인
        if sector and sector in strategy.get("min_signal_override", {}):
            min_signal_required = strategy["min_signal_override"][sector]

        if buy_count < min_signal_required:
            return False, f"신호 {buy_count}/12 < 필요신호 {min_signal_required}/12 (PC전략)"

        # 3) 업종 포커스 확인 (optional, 엄격하지 않음 — 강제는 아님)
        focus = strategy.get("focus_sectors", [])
        if focus and sector and sector not in focus:
            logger.debug("⚠️ %s: 포커스 업종 외 (%s) — 진행", sector, focus)

        return True, f"PC전략 승인 (신호{buy_count}≥{min_signal_required})"
    except Exception as e:
        logger.debug("전략 검증 실패: %s", e)
        return True, "전략 검증 오류 (진행)"


def _log_pc_stats():
    """📊 PC 호출 통계 로깅 (매시간)"""
    if not pc_director:
        return
    try:
        stats = pc_director.get_pc_stats()
        logger.info(
            "📊 PC 효율 통계: 신호변화 호출=%d건, 평균분석시간=%.2f초, "
            "누적시간=%.1f초, 상태=%s",
            stats["signal_shift_calls"],
            stats["avg_analysis_time_sec"],
            stats["total_time_sec"],
            stats["status"]
        )
    except Exception:
        logger.debug("PC 통계 로깅 실패")


def _log_holdings_status():
    """매시 정각 보유종목 수익률을 로그 파일에 기록 — 장중 에이전트가 읽을 수 있게."""
    import sqlite3 as _sqlite3
    from mock_trading.kis_client import get_current_price as get_price
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
        # 파일 크기 제한: 10,000줄 초과 시 앞 절반 삭제 (무한 증가 방지)
        if os.path.exists(log_path):
            with open(log_path, "r", encoding="utf-8") as f:
                existing = f.readlines()
            if len(existing) > 10000:
                with open(log_path, "w", encoding="utf-8") as f:
                    f.writelines(existing[-5000:])
        with open(log_path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        logger.info("holdings_hourly.log 기록 완료: %d종목", len(holdings))
    except Exception:
        logger.exception("_log_holdings_status 실패")


def auto_trade_loop():
    """자동매매 루프 — daemon 스레드
    - 신규매수 기회 있을 때: 30초 간격 (활성)
    - 신규매수 기회 없을 때: 5분 간격 (효율화, 매도만 모니터링)
    - PC LLM(관리자)이 당일 전략을 지시, Python(작업자)이 실행
    """
    _restore_today_trades()

    # PC LLM 디렉터 스레드 시작 (백그라운드에서 매일 09:00에 당일 전략 수립)
    if pc_director:
        try:
            t = threading.Thread(target=pc_director.director_scheduler, daemon=True)
            t.start()
            logger.info("🎯 PC LLM 디렉터 스레드 시작 (관리자 역할)")
        except Exception as e:
            logger.warning("PC LLM 디렉터 시작 실패: %s", e)

    logger.info("자동매매 루프 시작 (적응형 간격: 신규기회 있을 때 30초, 없을 때 5분)")
    logger.info("구조: PC LLM(관리자) → 전략 지시 → Python(작업자) → 실행")
    last_hourly_log = None
    no_buy_opportunity_count = 0  # 신규매수 기회 없음 연속 카운트
    _last_daily_report_date = None

    while True:
        try:
            bought = auto_trade_cycle()
            kst_now = datetime.datetime.now(pytz.timezone("Asia/Seoul"))
            if is_trading_hours():
                # 매시 정각 보유종목 로그 + PC 효율 통계
                if kst_now.minute == 0 and last_hourly_log != kst_now.hour:
                    _log_holdings_status()
                    _log_pc_stats()  # 📊 PC 부하 모니터링
                    last_hourly_log = kst_now.hour

            # 15:30~15:40 장 마감 일일 최종 요약
            if kst_now.hour == 15 and 30 <= kst_now.minute <= 40:
                if _last_daily_report_date != kst_now.date():
                    _last_daily_report_date = kst_now.date()
                    for _acc in _ACCOUNTS:
                        _tlog = getattr(config, _acc["log_attr"], [])
                        _buys = sum(1 for e in _tlog if "신규매수" in e)
                        _profit = sum(1 for e in _tlog if "전량매도" in e and "+" in e)
                        _loss = sum(1 for e in _tlog if "전량매도" in e and "-" in e)
                        _partial = sum(1 for e in _tlog if "부분매도" in e)
                        logger.info("[DAILY_REPORT] %s 매수:%d 익절:%d 손절:%d 부분매도:%d",
                                    _acc["label"], _buys, _profit, _loss, _partial)

            # 효율화: 신규매수 기회 체크
            if not bought:  # 신규매수 없음
                no_buy_opportunity_count += 1
            else:
                no_buy_opportunity_count = 0  # 매수 발생 시 리셋

        except Exception:
            logger.exception("auto_trade_cycle 예외")

        # 적응형 sleep: 신규매수 기회 없으면 5분, 있으면 30초
        import time
        if no_buy_opportunity_count >= 2:  # 2회 연속 기회 없음 = 5분으로 전환
            sleep_sec = 300
            if no_buy_opportunity_count == 2:
                logger.info("💤 신규매수 기회 없음 → 효율화: 30초 → 5분 간격으로 전환 (매도만 모니터링)")
        else:
            sleep_sec = 30

        time.sleep(sleep_sec)


def smart_wakeup_monitor():
    """
    Ollama 없이 Python만으로 순매수·차트 신호 변화 감지 → PC 자동 웨이크업.
    - 30초: 순매수 신규진입 체크 (DB 쿼리, 가벼움)
    - 5분:  차트 신호 변화 체크 (pykrx 호출, 무거워서 간격 유지)
    트리거:
      1) 순매수 워치리스트에 신규 종목 진입
      2) 상위 종목 buy_count 2 이상 급증 또는 BUY 임계(≥6) 신규 진입
    """
    import time as _t
    from llm_client import send_wol

    logger.info("스마트 웨이크업 모니터 시작 (순매수 30초 / 차트신호 5분)")
    # 시작 시 현재 워치리스트로 초기화 — 재시작 시 전체 신규 오탐 방지
    try:
        _prev_codes: set = {c for c, *_ in get_watchlist_from_db(months=3)}
        logger.info("스마트 웨이크업 초기 워치리스트: %d종목", len(_prev_codes))
    except Exception:
        _prev_codes: set = set()
    _prev_signals: dict = {}   # code → buy_count
    _last_chart_check = 0      # 마지막 차트 신호 체크 시각

    while True:
        _t.sleep(30)

        if not is_trading_hours():
            continue

        try:
            watchlist = get_watchlist_from_db(months=3)
            cur_codes = {c for c, *_ in watchlist}
            triggers = []

            # ── 1. 순매수 신규진입 (매 30초) ───────────────────
            new_entries = cur_codes - _prev_codes
            if new_entries:
                names = []
                for code, name, day_cnt, both in watchlist:
                    if code in new_entries:
                        tag = "⭐" if both else ""
                        names.append(f"{tag}{name}({code})")
                triggers.append(f"순매수 신규진입 {len(new_entries)}종목: {', '.join(names[:5])}")
            _prev_codes = cur_codes

            # ── 2. 차트 신호 변화 (5분 간격) ───────────────────
            now_ts = _t.time()
            if now_ts - _last_chart_check >= 300:
                _last_chart_check = now_ts
                top_candidates = [(c, n) for c, n, d, _ in sorted(watchlist, key=lambda x: -x[2])[:8]]
                for code, name in top_candidates:
                    try:
                        sig = calculate_chart_signals(code)
                        bc = sig.get("buy_count", 0)
                        prev_bc = _prev_signals.get(code, bc)
                        delta = bc - prev_bc
                        if delta >= 2 or (bc >= 6 and prev_bc < 6):
                            tag = "🆙" if delta >= 2 else "🔔"
                            triggers.append(f"{tag}{name}({code}) 신호 {prev_bc}→{bc}/12")
                        _prev_signals[code] = bc
                    except Exception:
                        pass

            if triggers:
                reason = "\n".join(triggers)
                logger.info("스마트 웨이크업 트리거: %s", reason)
                send_wol()
                # _tg_notify(f"🌟 [스마트 웨이크업]\n{reason}\n→ PC 깨우는 중...")  # 신호 메시지 제거
                # → 매매 완료 메시지만 수신

        except Exception:
            logger.exception("smart_wakeup_monitor 오류")


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
            f"총신호: {sig['buy_count']}/12 → {'🟢 BUY 후보' if sig['buy_count'] >= 6 else '🔴 신호 부족'}"
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
            df = _naver_net_buy_list(gubun, '01', 'buy', date_str=date_str)
            if df is None or df.empty:
                logger.warning("collect_smart_flows: %s 데이터 없음", investor_type)
                continue
            from db_utils import get_stock_code_from_db
            from stock_data import naver_search_code
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

def generate_chart_png(code: str, name: str, df_daily=None) -> str | None:
    """
    차트 PNG 생성 — 실제 HTS 설정 기반
    패널: 메인(종가+후행스팬+선행스팬1/2+MAC채널) | ADX(+PDI+MDI) | RSI(+Signal) | MACD(+Signal)
    """
    try:
        import pandas as pd
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.font_manager as fm
        import matplotlib.gridspec as gridspec
        import ta as _ta

        _font_path = "/usr/share/fonts/truetype/nanum/NanumBarunGothic.ttf"
        _fp = fm.FontProperties(fname=_font_path) if os.path.exists(_font_path) else None
        plt.rcParams['axes.unicode_minus'] = False

        # ── 데이터 준비 ─────────────────────────────────────────────
        if df_daily is not None and len(df_daily) >= 10:
            df = df_daily.copy()
            df.columns = [c.capitalize() for c in df.columns]
            if not isinstance(df.index, pd.DatetimeIndex):
                df.index = pd.to_datetime(df.index)
        else:
            import datetime
            today_str = datetime.date.today().strftime("%Y%m%d")
            from_str  = (datetime.date.today() - datetime.timedelta(days=130)).strftime("%Y%m%d")
            df = pykrx_stock.get_market_ohlcv(from_str, today_str, code)
            if df is None or len(df) < 10:
                return None
            df = df.rename(columns={"시가":"Open","고가":"High","저가":"Low","종가":"Close","거래량":"Volume"})
            df = df[["Open","High","Low","Close","Volume"]].copy()
            if not isinstance(df.index, pd.DatetimeIndex):
                df.index = pd.to_datetime(df.index)
        df = df.dropna()

        close = df["Close"]
        high  = df["High"]
        low   = df["Low"]
        x     = range(len(df))
        dates = [d.strftime("%m/%d") for d in df.index]
        tick_step = max(1, len(df) // 8)
        xticks  = list(x)[::tick_step]
        xlabels = dates[::tick_step]

        # ── 지표 계산 ────────────────────────────────────────────────
        # 일목균형표 (HTS 설정: 전환1 기준1 선행1=1 선행2=2 후행1)
        tenkan   = (high.rolling(1).max()  + low.rolling(1).min())  / 2   # 전환선(1)
        kijun    = (high.rolling(1).max()  + low.rolling(1).min())  / 2   # 기준선(1)
        senkou_b = ((high.rolling(2).max() + low.rolling(2).min()) / 2).shift(1)  # 선행스팬2
        chikou   = close.shift(-1)                                          # 후행스팬(1)

        # MAC 채널 (기간5, ±10%)
        mac_ma   = close.rolling(5).mean()
        mac_high = high.rolling(5).mean()
        mac_low  = low.rolling(5).mean()
        mac_upper = mac_ma * 1.10
        mac_lower = mac_ma * 0.90

        # RSI(6) + Signal(6)
        rsi        = ta.momentum.rsi(close, window=6)
        rsi_signal = rsi.rolling(6).mean()

        # MACD(5,13,6)
        _macd_obj = ta.trend.MACD(close, window_fast=5, window_slow=13, window_sign=6)
        macd_line = _macd_obj.macd()
        macd_sig  = _macd_obj.macd_signal()

        # ADX(3) + PDI + MDI
        adx_ind = _ta.trend.ADXIndicator(high, low, close, window=3)
        adx     = adx_ind.adx()
        pdi     = adx_ind.adx_pos()
        mdi     = adx_ind.adx_neg()

        # ── 레이아웃 ─────────────────────────────────────────────────
        fig = plt.figure(figsize=(14, 14), facecolor='white')
        gs  = gridspec.GridSpec(5, 1, height_ratios=[4, 0.8, 1.5, 1.5, 1.5], hspace=0.04)

        _lw = {"lw": 1.0}

        # ── 1. 메인 패널 ─────────────────────────────────────────────
        ax1 = fig.add_subplot(gs[0])
        # MAC 채널
        ax1.plot(x, mac_upper, color='skyblue',  linewidth=0.8, label='MAC Upper', alpha=0.9)
        ax1.plot(x, mac_lower, color='skyblue',  linewidth=0.8, label='MAC Lower', alpha=0.9)
        ax1.plot(x, mac_high,  color='orange',   linewidth=0.8, label='High MA',   alpha=0.8)
        ax1.plot(x, mac_low,   color='orange',   linewidth=0.8, label='Low MA',    alpha=0.8)
        ax1.fill_between(x, mac_upper, mac_lower, color='skyblue', alpha=0.07)
        # 기준선 (녹색), 선행스팬2 (보라)  — 선행스팬1·후행스팬 제거
        ax1.plot(x, kijun,   color='green',  linewidth=1.0, label='기준선', alpha=0.9)
        ax1.plot(x, senkou_b, color='purple', linewidth=0.9, label='선행2',  alpha=0.9)
        # 종가 라인
        ax1.plot(x, close, color='red', linewidth=1.2, label='종가')
        _tk = {"fontproperties": _fp, "fontsize": 12} if _fp else {"fontsize": 12}
        ax1.set_title(f"{name}({code})", **_tk)
        ax1.legend(loc='upper left', fontsize=6, prop=_fp, ncol=4)
        ax1.set_xticks(xticks); ax1.set_xticklabels([])
        ax1.grid(True, alpha=0.2)

        # ── 2. 거래량 패널 ───────────────────────────────────────────
        ax2 = fig.add_subplot(gs[1], sharex=ax1)
        vol_colors = ['red' if c >= o else 'blue'
                      for c, o in zip(df['Close'], df['Open'])]
        ax2.bar(x, df['Volume'], color=vol_colors, alpha=0.6, width=0.8)
        ax2.set_ylabel('Vol', fontsize=7)
        ax2.set_xticks(xticks); ax2.set_xticklabels([])
        ax2.yaxis.set_major_formatter(
            plt.FuncFormatter(lambda v, _: f"{int(v/1000)}K" if v >= 1000 else str(int(v))))
        ax2.grid(True, alpha=0.15)

        # ── 3. ADX 패널 ──────────────────────────────────────────────
        ax3 = fig.add_subplot(gs[2], sharex=ax1)
        ax3.plot(x, adx, color='green', **_lw, label='ADX')
        ax3.plot(x, pdi, color='red',   **_lw, label='PDI')
        ax3.plot(x, mdi, color='blue',  **_lw, label='MDI')
        ax3.axhline(7, color='red', linewidth=0.6, linestyle='--')
        ax3.legend(loc='upper left', fontsize=6, ncol=3)
        ax3.set_ylabel('ADX', fontsize=8)
        ax3.set_xticks(xticks); ax3.set_xticklabels([])
        ax3.grid(True, alpha=0.2)

        # ── 4. RSI 패널 ──────────────────────────────────────────────
        ax4 = fig.add_subplot(gs[3], sharex=ax1)
        ax4.plot(x, rsi,        color='red',   **_lw, label='RSI(6)')
        ax4.plot(x, rsi_signal, color='green', **_lw, label='Signal(6)')
        ax4.axhline(70, color='gray', linewidth=0.5, linestyle='--')
        ax4.axhline(30, color='gray', linewidth=0.5, linestyle='--')
        ax4.set_ylim(0, 100)
        ax4.legend(loc='upper left', fontsize=6, ncol=2)
        ax4.set_ylabel('RSI', fontsize=8)
        ax4.set_xticks(xticks); ax4.set_xticklabels([])
        ax4.grid(True, alpha=0.2)

        # ── 5. MACD 패널 ─────────────────────────────────────────────
        ax5 = fig.add_subplot(gs[4], sharex=ax1)
        ax5.plot(x, macd_line, color='red',   **_lw, label='MACD(5,13)')
        ax5.plot(x, macd_sig,  color='green', **_lw, label='Signal(6)')
        ax5.axhline(0, color='gray', linewidth=0.5)
        ax5.legend(loc='upper left', fontsize=6, ncol=2)
        ax5.set_ylabel('MACD', fontsize=8)
        ax5.set_xticks(xticks); ax5.set_xticklabels(xlabels, fontsize=7)
        ax5.grid(True, alpha=0.2)

        chart_path = os.path.join(os.path.dirname(__file__), f"chart_{code}.png")
        fig.savefig(chart_path, bbox_inches='tight', dpi=100)
        plt.close(fig)
        return chart_path
    except Exception as e:
        logger.warning("차트 PNG 생성 실패 %s: %s", code, e)
        return None


def analyze_chart_for_chat(query: str) -> tuple:
    """
    채팅에서 종목명/코드로 차트 기술적 분석 요청 시 호출.
    차트 PNG 생성 → Ollama 비전 분석 → BUY/HOLD/SELL 판단 반환.
    반환: (텍스트, chart_path or None)
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
            return f"❌ '{query}' 종목을 찾을 수 없어요. 6자리 코드로 다시 시도해보세요.", None
        name = query

    try:
        sig = calculate_chart_signals(code)
    except Exception as _e:
        logger.warning("calculate_chart_signals 예외 %s: %s", code, _e)
        sig = None
    if not sig:
        return f"❌ {name}({code}) 차트 데이터 조회 실패\n거래소 응답 지연일 수 있어요. 잠시 후 다시 시도해주세요.", None

    s = sig.get("signals", {})
    def v(k): return "✅" if s.get(k) else "❌"

    signal_summary = (
        f"종목: {name}({code}) | 총신호: {sig['buy_count']}/12\n\n"
        f"월봉: 기준선{'위(✅)' if s.get('월봉_일목균형표') else '아래(❌)'} ADX{v('월봉_ADX')} RSI{v('월봉_RSI')} MACD{v('월봉_MACD')}\n"
        f"주봉: 기준선{'위(✅)' if s.get('주봉_일목균형표') else '아래(❌)'} ADX{v('주봉_ADX')} RSI{v('주봉_RSI')} MACD{v('주봉_MACD')}\n"
        f"일봉: 기준선{'위(✅)' if s.get('일봉_일목균형표') else '아래(❌)'} ADX{v('일봉_ADX')} RSI{v('일봉_RSI')} MACD{v('일봉_MACD')} "
        f"정배열{v('일봉_정배열')} 가격>MA20{v('일봉_가격위치')}\n"
        f"분봉(15/30/60): 일목{v('분봉_일목균형표')} ADX{v('분봉_ADX')} RSI{v('분봉_RSI')} MACD{v('분봉_MACD')}\n"
        f"단타(3분): 일목{v('분봉_3분_일목균형표')} ADX{v('분봉_3분_ADX')} RSI{v('분봉_3분_RSI')} MACD{v('분봉_3분_MACD')}\n"
        f"ADX={sig.get('adx','?'):.1f} MACD히스트={sig.get('macd_hist','?')}\n\n"
    )

    # Ollama 자율 학습 기법 조회 (chart_method_memory)
    rag_method = ""
    try:
        from rag_store import search_chart_method
        sector = next((v for k, v in {
            "005930": "반도체", "000660": "반도체",
            "005380": "자동차", "068270": "바이오",
            "035720": "IT플랫폼", "105560": "금융",
        }.items() if k == code), "전체")
        method_query = f"{sector} 차트분석 기법 상승 하락 일목균형 월봉 주봉"
        rag_method = search_chart_method(method_query, n_results=2)
    except Exception as _me:
        logger.debug("기법 RAG 조회 실패: %s", _me)

    # RAG 과거 패턴 조회 (chart_pattern_memory)
    rag_pattern = ""
    try:
        from rag_store import search_chart_pattern
        # 현재 신호 상황을 쿼리 텍스트로 변환
        s = sig.get("signals", {})
        def _pos(tf, line):
            # price_vs_kijun 방향 텍스트 생성
            ichi = s.get(f"{tf}_일목균형표")
            rsi  = s.get(f"{tf}_rsi_val")
            macd = s.get(f"{tf}_macd_val")
            return (f"{line}: 기준선{'위' if ichi else '아래'} "
                    f"RSI:{rsi:.0f}" if rsi else f"{line}: 기준선{'위' if ichi else '아래'}")
        rag_query = (
            f"종목:{name} "
            f"월봉:기준선{'위' if s.get('월봉_일목균형표') else '아래'} "
            f"주봉:기준선{'위' if s.get('주봉_일목균형표') else '아래'} "
            f"일봉:기준선{'위' if s.get('일봉_일목균형표') else '아래'} "
            f"MA:{'정배열' if s.get('일봉_정배열') else '역배열'} "
            f"신호:{sig['buy_count']}/12"
        )
        rag_pattern = search_chart_pattern(rag_query, n_results=3)
    except Exception as _re2:
        logger.debug("RAG 패턴 조회 실패: %s", _re2)

    # 차트 PNG 생성 → Ollama 비전 분석
    chart_path = generate_chart_png(code, name, df_daily=sig.get("df_daily"))
    if not chart_path:
        logger.warning("generate_chart_png 실패: %s", code)
    if chart_path:
        try:
            from llm_client import call_mistral_vision
            vision_prompt = (
                f"이 종목은 {name}({code})이야. 아래 기술적 신호 데이터도 참고해.\n\n"
                f"{signal_summary}\n"
                "## 차트 구성 (5패널) — 반드시 이 설명을 기준으로 분석할 것\n"
                "- 패널1(메인): 종가 라인(빨강) + 기준선(녹색, 1기간) + 선행스팬2(보라) + MAC채널(하늘색 상/하한±10%, 주황 고/저MA)\n"
                "  ※ 이 차트에는 선행스팬1·후행스팬이 없습니다. 녹색=기준선, 보라=선행스팬2 — 이 2가지만 존재. '스팬1'·'후행스팬'은 절대 언급하지 마세요.\n"
                "- 패널2: 거래량 (양봉=빨강, 음봉=파랑)\n"
                "- 패널3: ADX(녹색)/PDI(빨강)/MDI(파랑), 기준선7\n"
                "- 패널4: RSI(6,빨강)/Signal(녹색), 기준선30·70\n"
                "- 패널5: MACD(5,13,빨강)/Signal(6,녹색), 기준선0\n\n"
                + (f"## Ollama 학습 기법 (참고용 — 위 차트 구성 우선)\n{rag_method}\n\n" if rag_method else "")
                + (f"## 과거 유사 패턴\n{rag_pattern}\n\n" if rag_pattern else "")
                +
                "## 분석 방법\n"
                "위 학습 기법을 현재 차트에 직접 적용해서 분석해줘.\n"
                "기법에서 '스팬1'·'후행스팬'을 언급해도 이 차트에는 없으니 무시하고, 기준선/선행스팬2로만 판단해.\n"
                "기법에서 말하는 조건이 지금 신호와 일치하는지 하나씩 대조하고:\n"
                "1. 추세 — 월봉/주봉/일봉 각각 기준선(녹색)·선행스팬2(보라) 위치 관계를 타임프레임별로 구분해서 서술. 절대 뭉뚱그리지 말 것.\n"
                "2. 구간 판단 — 상승초입/상승중/고점권/하락초입/하락중/바닥권 중 어디?\n"
                "3. 핵심 근거 — 기법과 일치하는 신호 / 기법과 다른 신호\n"
                "4. 결론 — 매수/관망/매도 + 진입or청산 타이밍"
            )
            vision_result = call_mistral_vision(vision_prompt, chart_path)
            if vision_result and len(vision_result) > 30:
                # 베이스 모델 환각 후처리 — 이 차트에 없는 선행스팬1·후행스팬 언급 제거
                vision_result = _RE_SPAN1.sub('선행스팬2', vision_result)
                vision_result = _RE_SPAN_1.sub('선행스팬2', vision_result)
                vision_result = _RE_HUIHAENG.sub('기준선', vision_result)
                return (
                    f"📊 {name}({code}) 차트 분석 (비전)\n\n"
                    f"{signal_summary}"
                    f"{vision_result}"
                ), chart_path
        except Exception as e:
            logger.warning("비전 분석 실패 %s: %s", code, e)

    # 폴백: 텍스트 신호 기반 Ollama 분석
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
            ), chart_path
    except Exception:
        logger.warning("analyze_chart_for_chat Ollama 판단 실패 %s", code)

    # 폴백: 신호 수로 단순 판단 (BUY>=6, HOLD 4-5, SELL<4 — /12 기준)
    cnt = sig['buy_count']
    if cnt >= 6:
        action_str = "📈 매수"
    elif cnt >= 4:
        action_str = "⏸ 관망"
    else:
        action_str = "📉 매도"
    return (
        f"📊 {name}({code}) 차트 분석\n\n"
        f"{signal_summary}"
        f"판단: {action_str} (신호 {cnt}/12 기준)"
    ), chart_path


def get_watchlist_from_db(months: int = 3, days: int = None) -> list:
    """
    DB mock_smart_flows에서 최근 N개월(또는 N일)간 외국인 또는 기관 순매수 등장 종목 반환.
    동시 등장(⭐) 여부도 표시. 등장 횟수 내림차순 정렬.
    반환: [(code, name, days, both)] — both=True면 외국인+기관 동시
    """
    p = get_db_pool()
    if not p:
        return []
    try:
        with p.acquire() as conn:
            with conn.cursor() as cur:
                if days is not None:
                    where_clause = "WHERE collected_at >= SYSTIMESTAMP - :1"
                    param = [days]
                else:
                    where_clause = "WHERE collected_at >= ADD_MONTHS(SYSTIMESTAMP, :1)"
                    param = [-months]
                cur.execute(
                    f"SELECT ticker, name, COUNT(DISTINCT date_str) AS days, "
                    f"COUNT(DISTINCT investor_type) AS inv_cnt "
                    f"FROM mock_smart_flows "
                    f"{where_clause} "
                    f"AND REGEXP_LIKE(ticker, '^[0-9]{{6}}$') "
                    f"GROUP BY ticker, name "
                    f"ORDER BY inv_cnt DESC, days DESC",
                    param
                )
                rows = cur.fetchall()
        result = []
        seen = set()
        for ticker, name, day_cnt, inv_cnt in rows:
            if ticker not in seen:
                seen.add(ticker)
                result.append((ticker, name, day_cnt, inv_cnt >= 2))
        return result
    except Exception:
        logger.exception("get_watchlist_from_db 오류")
        return []


def _make_scan_reason(sig: dict) -> str:
    """신호수 기반 간결한 판단 근거 생성 (Ollama 없이)."""
    s = sig.get("signals", {})
    parts = []
    monthly = all(s.get(f"월봉_{k}") for k in ["일목균형표","ADX","RSI","MACD"])
    weekly  = all(s.get(f"주봉_{k}") for k in ["일목균형표","ADX","RSI","MACD"])
    daily   = all(s.get(f"일봉_{k}") for k in ["일목균형표","ADX","RSI","MACD"])
    if monthly and weekly and daily:
        parts.append("월/주/일봉 전봉 신호 일치")
    elif monthly and weekly:
        parts.append("월봉·주봉 신호 일치")
    elif weekly and daily:
        parts.append("주봉·일봉 신호 일치")
    elif daily:
        parts.append("일봉 신호 강함")
    if s.get("일봉_정배열"):
        parts.append("이평 정배열")
    if s.get("일봉_가격위치"):
        parts.append("가격>MA20")
    rsi = sig.get("rsi", 0)
    if rsi and rsi > 50:
        parts.append(f"RSI{rsi:.0f}")
    return " · ".join(parts) if parts else f"신호 {sig['buy_count']}/12"


def scan_buy_signals_for_chat(months: int = 3, days: int = None) -> str:
    """
    채팅용 — DB 누적 N개월(또는 N일)간 외국인+기관 동시 순매수 종목 워치리스트 기반 매수 신호 스캔.
    days 지정 시 days 우선 적용.
    """
    period_label = f"{days}일" if days is not None else f"{months}개월"
    watchlist = get_watchlist_from_db(months=months, days=days)

    # 오늘 실시간 데이터로 보강 (DB에 없는 신규 종목 추가)
    foreign = _scrape_naver_codes("9000", limit=20)
    inst_set = set(_scrape_naver_codes("1000", limit=20))
    existing_codes = {c for c, _, __, ___ in watchlist}
    for code in set(foreign) | inst_set:
        if code not in existing_codes:
            name = _get_name_by_code(code) or code
            both = code in foreign and code in inst_set
            watchlist.append((code, name, 1, both))

    candidates = watchlist

    if not candidates:
        return "순매수 워치리스트가 비어있습니다."

    def _scan_one(item):
        code, name, day_cnt, both = item
        sig = calculate_chart_signals(code, scan_mode=True)  # 분봉 스킵
        if not sig:
            return None
        # 신호수 룰 기반 판단 (Ollama 호출 없음)
        cnt = sig["buy_count"]
        if cnt >= 6:
            action = "BUY"
            trade_type = "스윙"
            reason = _make_scan_reason(sig)
        elif cnt >= 4:
            action = "HOLD"
            trade_type = "스윙"
            reason = _make_scan_reason(sig)
        else:
            action = "SELL"
            trade_type = "스윙"
            reason = "신호 부족"
        decision = {"action": action, "trade_type": trade_type, "reason": reason}
        return (code, name, day_cnt, both, sig, decision)

    from concurrent.futures import ThreadPoolExecutor, as_completed
    results_buy, results_hold, results_sell = [], [], []
    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = {ex.submit(_scan_one, item): item for item in candidates}
        for fut in as_completed(futures):
            entry = fut.result()
            if entry is None:
                continue
            cnt = entry[4]["buy_count"]
            if entry[5]["action"] == "BUY":
                results_buy.append(entry)
            elif cnt < 4:
                results_sell.append(entry)
            else:
                results_hold.append(entry)

    # 신호 수 내림차순 정렬
    results_buy.sort(key=lambda x: x[4]["buy_count"], reverse=True)
    results_hold.sort(key=lambda x: x[4]["buy_count"], reverse=True)
    results_sell.sort(key=lambda x: x[4]["buy_count"], reverse=True)

    lines = [f"📊 외국인+기관 워치리스트 {len(candidates)}종목 스캔 (최근 {period_label} 누적)\n"]

    def _one_line(code, name, day_cnt, both, sig, decision):
        from mock_trading.kis_client import is_nxt_supported
        from auto_trader import is_nxt_hours
        star   = "⭐" if both else "  "
        tt     = decision.get("trade_type", "스윙")
        reason = (decision.get("reason") or "")[:80]
        bc     = sig["buy_count"]
        nxt_tag = ""
        if is_nxt_hours():
            nxt_tag = " [KRX+NXT]" if is_nxt_supported(code) else " [KRX전용]"
        return f"{star}{name}({code}) {bc}/12 [{tt}]{nxt_tag} 누적{day_cnt}일 — {reason}"

    if results_buy:
        lines.append(f"✅ 매수 신호 ({len(results_buy)}개)")
        for entry in results_buy:
            lines.append(_one_line(*entry))
    else:
        lines.append("✅ 매수 신호 없음")

    if results_hold:
        lines.append(f"\n⏸ 관망 ({len(results_hold)}개)")
        for entry in results_hold:
            lines.append(_one_line(*entry))

    if results_sell:
        lines.append(f"\n📉 매도 ({len(results_sell)}개 — 신호 4 미만)")
        for entry in results_sell:
            lines.append(_one_line(*entry))

    return "\n".join(lines)
