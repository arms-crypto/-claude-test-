#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🎯 PC LLM 전략 디렉터 (관리자)
- 장 시작 시: 당일 매매 전략 수립
- 장중: 신호 변화 감시 및 전술 조정
- 장 마감 후: 성과 분석 및 내일 전략 수립

관계: PC LLM(관리자) ← 전략 지시 → Python(작업자)
"""

import json
import os
import datetime
import pytz
import requests
import logging
import threading
import time

logger = logging.getLogger("pc_director")

# PC Ollama 설정 (관리자) — Gemma 4 26b @ LM Studio
PC_OLLAMA_URL = "http://221.144.111.116:8000/v1/chat/completions"
PC_MODEL = "google_gemma-4-26b-a4b-it"  # Gemma 4 26B
PC_OLLAMA_HEADERS = {"Authorization": "Bearer sk-lm-65FGVrPT:vqn138RmtIy3Br0867pZ"}

# 전략 저장 경로
STRATEGY_PATH = os.path.join(os.path.dirname(__file__), "daily_strategy.json")

# 전략 캐시 (메모리)
_current_strategy = {
    "date": None,
    "status": "not_initialized",
    "focus_sectors": [],
    "min_signal_override": {},  # sector → min_signal
    "risk_level": "normal",     # low / normal / high
    "max_holdings": 7,
    "notes": ""
}

# 📊 모니터링: PC 부하 관리
_pc_stats = {
    "signal_shift_calls": 0,      # 신호 변화 분석 호출 횟수
    "last_call_time": None,       # 마지막 호출 시간
    "total_analysis_time": 0,     # 누적 분석 시간 (초)
}

# 🤖 완전 자율 관리자 — 실행 대기 큐
_pending_manager_actions: list = []
_last_system_review: datetime.datetime | None = None

_lock = threading.Lock()


def _call_pc_director(prompt: str) -> str:
    """PC LLM 호출 (gemma2:27b-instruct)"""
    try:
        response = requests.post(
            PC_OLLAMA_URL,
            headers=PC_OLLAMA_HEADERS,
            json={
                "model": PC_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
                "max_tokens": 1000,
            },
            timeout=120,
            proxies={"http": None, "https": None}
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.error("PC LLM 호출 실패: %s", e)
        return ""


def init_daily_strategy():
    """
    장 시작 시 1회만 호출.
    PC LLM이 당일 시장 상황을 분석해 전략 수립.
    """
    global _current_strategy

    kst_now = datetime.datetime.now(pytz.timezone("Asia/Seoul"))
    today = kst_now.date().isoformat()

    with _lock:
        # 이미 오늘 전략 수립했으면 스킵
        if _current_strategy["date"] == today and _current_strategy["status"] == "ready":
            logger.info("📋 오늘 전략 이미 수립됨: %s", json.dumps(_current_strategy, ensure_ascii=False))
            return _current_strategy

    logger.info("🎯 PC LLM 당일 전략 수립 중...")

    # 시장 정보 수집 (간단한 버전)
    market_info = _get_market_context()

    prompt = f"""당신은 한국 주식 자동매매 시스템의 전략 관리자입니다.

[오늘 시장 상황]
{market_info}

[지금까지 누적 매매 성과]
- 현재 보유: ~7종목
- 평가손익: 약 +3.5%
- 일일 신호: 12신호 기반 매매 규칙 사용

[오늘 매매 전략을 JSON으로만 작성하세요]
{{
  "focus_sectors": ["반도체", "에너지"],  // 오늘 집중할 업종 (2-3개)
  "min_signal_override": {{"반도체": 6, "에너지": 7}},  // 업종별 신호 임계값 조정 (선택)
  "risk_level": "normal",  // low (보수적) / normal (기본) / high (공격적)
  "max_holdings": 7,       // 최대 보유 종목
  "notes": "상황 설명"
}}

주의:
- JSON만 반환 (다른 텍스트 금지)
- risk_level: low(신호≥7) / normal(신호≥6) / high(신호≥5)
- 어제 패턴 재활용 금지, 오늘 시장 맥락에 맞게 수립
"""

    response = _call_pc_director(prompt)
    if not response:
        logger.warning("PC LLM 응답 없음, 기본 전략 사용")
        _current_strategy = {
            "date": today,
            "status": "ready",
            "focus_sectors": ["반도체", "에너지"],
            "min_signal_override": {},
            "risk_level": "normal",
            "max_holdings": 7,
            "notes": "PC LLM 응답 없음 — 기본 전략"
        }
    else:
        try:
            import re
            match = re.search(r'\{[\s\S]*\}', response)
            if match:
                strategy = json.loads(match.group())
                _current_strategy = {
                    "date": today,
                    "status": "ready",
                    **strategy
                }
            else:
                _current_strategy["status"] = "failed"
                _current_strategy["notes"] = "JSON 파싱 실패"
        except Exception as e:
            logger.error("전략 파싱 실패: %s", e)
            _current_strategy["status"] = "failed"

    # 파일 저장
    _save_strategy()
    logger.info("✅ 당일 전략 수립 완료: %s", json.dumps(_current_strategy, ensure_ascii=False))
    return _current_strategy


def get_current_strategy() -> dict:
    """현재 전략 반환 (읽기 전용)."""
    with _lock:
        return dict(_current_strategy)


def get_pc_stats() -> dict:
    """📊 PC 호출 통계 반환 (모니터링용)."""
    with _lock:
        avg_time = (_pc_stats["total_analysis_time"] / _pc_stats["signal_shift_calls"]
                   if _pc_stats["signal_shift_calls"] > 0 else 0)
        return {
            "signal_shift_calls": _pc_stats["signal_shift_calls"],
            "avg_analysis_time_sec": round(avg_time, 2),
            "total_time_sec": round(_pc_stats["total_analysis_time"], 1),
            "last_call": _pc_stats["last_call_time"],
            "status": "active" if _pc_stats["signal_shift_calls"] > 0 else "idle"
        }


def _get_market_context() -> str:
    """거시지표 6종 조회 (KOSPI/KOSDAQ/환율/유가/나스닥/미10Y금리)."""
    try:
        from stock_data import get_macro_indicators
        return get_macro_indicators()
    except Exception as e:
        logger.debug("시장 정보 조회 실패: %s", e)
        return "시장 정보 조회 실패"


def _collect_holdings_news() -> str:
    """보유 종목(트레이너 계좌 기준)별 최신 뉴스 수집."""
    import sqlite3 as _sq
    base = os.path.dirname(os.path.abspath(__file__))
    db_path = os.path.join(base, "mock_trading/portfolio.db")
    try:
        con = _sq.connect(db_path)
        holdings = con.execute(
            "SELECT name, ticker FROM portfolio WHERE qty > 0"
        ).fetchall()
        con.close()
    except Exception:
        return "보유종목 조회 실패"

    if not holdings:
        return "보유종목 없음"

    try:
        from stock_data import naver_news
    except Exception:
        return "뉴스 함수 로드 실패"

    lines = []
    for name, ticker in holdings[:7]:  # 최대 7종목
        try:
            news = naver_news(f"{name} 주가")
            lines.append(f"[{name}({ticker})] {news}")
        except Exception:
            lines.append(f"[{name}({ticker})] 뉴스 조회 실패")
    return "\n".join(lines)


def analyze_signal_shift(code: str, name: str, prev_count: int, new_count: int, signals: dict) -> int:
    """
    🆕 신호 변화(shift) 감지 시 PC LLM이 추천 min_signal만 반환.
    단순하고 명확함 — Python이 그냥 적용하기만 하면 됨.

    Args:
        code: 종목코드
        name: 종목명
        prev_count: 이전 신호수
        new_count: 현재 신호수
        signals: 기술 신호 딕셔너리

    Returns:
        min_signal 값 (4~7) 또는 None (분석 실패)
        예: 5 → Python이 sector_params에 min_signal=5 적용
    """
    global _pc_stats

    delta = new_count - prev_count
    direction = "📈" if delta > 0 else "📉"

    s = signals or {}
    def v(k): return "✅" if s.get(k) else "❌"

    prompt = f"""[신호 변화 → min_signal 조절]

종목: {name}({code})
신호 변화: {prev_count}/12 → {new_count}/12 ({direction} {abs(delta)})

📊 신호 상태:
- 월봉: 일목{v('월봉_일목균형표')} ADX{v('월봉_ADX')} RSI{v('월봉_RSI')} MACD{v('월봉_MACD')}
- 주봉: 일목{v('주봉_일목균형표')} ADX{v('주봉_ADX')} RSI{v('주봉_RSI')} MACD{v('주봉_MACD')}
- 일봉: 일목{v('일봉_일목균형표')} ADX{v('일봉_ADX')} RSI{v('일봉_RSI')} MACD{v('일봉_MACD')}

[숫자만 반환]
4, 5, 6, 또는 7 (추천 min_signal 값)

기준:
- 4~5: 신호 변화 강하고 신뢰 높음
- 6: 중간 (신호 임계값이 높아야 함)
- 7: 신호 변화 약하거나 신뢰 낮음

**숫자만 반환하세요. 다른 텍스트 금지.**"""

    import time as _time
    start_time = _time.time()
    response = _call_pc_director(prompt)
    elapsed = _time.time() - start_time

    # 📊 호출 통계 기록
    with _lock:
        _pc_stats["signal_shift_calls"] += 1
        _pc_stats["last_call_time"] = datetime.datetime.now().isoformat()
        _pc_stats["total_analysis_time"] += elapsed

    if not response:
        logger.debug("📊 PC조절: %s [응답없음] %.1fs", name, elapsed)
        return None

    try:
        # 숫자만 추출
        import re
        match = re.search(r'[4-7]', response.strip())
        if match:
            min_signal = int(match.group())
            logger.info("📊 PC조절: %s → min_signal=%d (%.1fs)", name, min_signal, elapsed)
            return min_signal
    except:
        pass

    logger.debug("📊 PC조절: %s [파싱실패] %.1fs", name, elapsed)
    return None


def analyze_buy_signal(code: str, name: str, signals: dict, buy_count: int) -> str:
    """
    🆕 신규 BUY 신호 감지 시 PC LLM 분석 (필요할 때만 호출).

    Args:
        code: 종목코드
        name: 종목명
        signals: 기술 신호 딕셔너리
        buy_count: 신호수 (예: 7/12)

    Returns:
        PC LLM의 분석/판단 문구
    """
    s = signals or {}
    def v(k): return "✅" if s.get(k) else "❌"

    prompt = f"""[신규 BUY 신호 감지]

종목: {name}({code})
신호: {buy_count}/12

📊 기술 신호:
- 월봉: 일목{v('월봉_일목균형표')} ADX{v('월봉_ADX')} RSI{v('월봉_RSI')} MACD{v('월봉_MACD')}
- 주봉: 일목{v('주봉_일목균형표')} ADX{v('주봉_ADX')} RSI{v('주봉_RSI')} MACD{v('주봉_MACD')}
- 일봉: 일목{v('일봉_일목균형표')} ADX{v('일봉_ADX')} RSI{v('일봉_RSI')} MACD{v('일봉_MACD')}

[질문]
1. 이 신호가 신뢰할 만한가? (강점/약점)
2. 스윙/단타 중 어느 전략이 적합한가?
3. 추천 수익률 목표는?

답변은 간결하게."""

    return _call_pc_director(prompt)


def analyze_sell_signal(code: str, name: str, pnl: float, reason: str) -> str:
    """
    🆕 매도 신호/손절 감지 시 PC LLM 판단 (필요할 때만 호출).

    Args:
        code: 종목코드
        name: 종목명
        pnl: 수익률 (%)
        reason: 매도 이유

    Returns:
        PC LLM의 조언
    """
    emoji = "💰" if pnl >= 0 else "🔴"

    prompt = f"""[매도 신호 감지]

종목: {name}({code})
수익률: {pnl:+.1f}%
사유: {reason}

[질문]
1. 지금이 매도 타이밍인가?
2. 다시 진입할 가능성은?
3. 이 사례에서 배울 점은?

답변은 간결하게."""

    return _call_pc_director(prompt)


def report_trades_to_director(trades_summary: dict):
    """
    매매 결과를 PC LLM에 보고.
    trades_summary: {"bought": [...], "sold": [...], "pnl": +2.5, ...}
    """
    kst_now = datetime.datetime.now(pytz.timezone("Asia/Seoul"))

    if not is_trading_hours():
        return  # 장 외시간 보고 제외

    prompt = f"""[자동매매 시스템 실행 보고]

시간: {kst_now.strftime('%H:%M')}
매매 결과:
- 신규 매수: {', '.join(trades_summary.get('bought', []))}
- 부분/전량 매도: {', '.join(trades_summary.get('sold', []))}
- 당일 수익률: {trades_summary.get('pnl', 0):+.1f}%

[지시사항]
- 계속 현재 전략 유지? (Y)
- 아니면 조정 필요? (조정 사항 설명)

답변: Y 또는 조정 내용"""

    response = _call_pc_director(prompt)
    if "조정" in response or "변경" in response:
        logger.warning("PC LLM 전술 조정 요청: %s", response[:100])
        # TODO: 실시간 전술 조정 처리
    else:
        logger.info("PC LLM 피드백: 계속 진행")


def generate_evening_analysis(daily_trades: list):
    """
    장 마감 후 당일 성과 분석 및 내일 전략 초안.
    daily_trades: [(code, name, action, pnl, time), ...]
    """
    logger.info("🌆 PC LLM 저녁 분석 시작...")

    trades_text = "\n".join([
        f"  {name}({code}) {action} {pnl:+.1f}% @ {time}"
        for code, name, action, pnl, time in daily_trades
    ])

    prompt = f"""[당일 매매 분석]

{trades_text}

[분석 항목]
1. 오늘 신호 품질 평가 (높음/중간/낮음)
2. 수익성 개선 포인트 (3가지)
3. 내일 유의사항 (시장 리스크, 중요 이벤트 등)

답변은 간결하게 작성하세요."""

    response = _call_pc_director(prompt)
    logger.info("📊 저녁 분석 결과:\n%s", response)
    return response


def _collect_portfolio() -> str:
    """두 계좌 포트폴리오 상태 수집."""
    import sqlite3 as _sq
    base = os.path.dirname(os.path.abspath(__file__))
    accounts = [
        ("🔵 트레이너", os.path.join(base, "mock_trading/portfolio.db")),
        ("🟡 KY",       os.path.join(base, "mock_trading/portfolio_ky.db")),
    ]
    lines = []
    for label, db_path in accounts:
        try:
            con = _sq.connect(db_path)
            holdings = con.execute(
                "SELECT name, ticker, qty, avg_price FROM portfolio WHERE qty > 0"
            ).fetchall()
            cash_row = con.execute("SELECT value FROM account WHERE key='cash'").fetchone()
            con.close()
            cash = float(cash_row[0]) if cash_row else 0
            lines.append(f"{label} 예수금: {cash:,.0f}원 | 보유 {len(holdings)}종목")
            for name, ticker, qty, avg in holdings:
                lines.append(f"  {name}({ticker}) {qty}주 평단{avg:,.0f}원")
        except Exception as e:
            lines.append(f"{label} 조회 실패: {e}")
    return "\n".join(lines)


def _collect_today_stats() -> str:
    """당일 매매 통계 (트레이너 기준)."""
    import sqlite3 as _sq
    base = os.path.dirname(os.path.abspath(__file__))
    db_path = os.path.join(base, "mock_trading/portfolio.db")
    try:
        con = _sq.connect(db_path)
        today = datetime.datetime.now(pytz.timezone("Asia/Seoul")).date().isoformat()
        rows = con.execute(
            "SELECT action, name, pnl FROM trades WHERE DATE(created_at)=? ORDER BY id",
            [today]
        ).fetchall()
        con.close()
        sells = [(r[1], r[2]) for r in rows if r[0] == "SELL" and r[2] is not None]
        buys  = [r[1] for r in rows if r[0] == "BUY"]
        if not sells:
            return f"당일 매수: {len(buys)}건 | 당일 매도: 0건"
        wins   = [p for _, p in sells if p > 0]
        losses = [p for _, p in sells if p <= 0]
        wr     = len(wins) / len(sells) * 100
        avg_pnl = sum(p for _, p in sells) / len(sells)
        return (
            f"당일 매수: {len(buys)}건 | 당일 매도: {len(sells)}건\n"
            f"승률: {wr:.0f}% ({len(wins)}승 {len(losses)}패) | 평균손익: {avg_pnl:+.2f}%"
        )
    except Exception as e:
        return f"당일 통계 조회 실패: {e}"


def _collect_error_summary() -> str:
    """proxy_v54.log 최근 에러 요약 (최대 5건)."""
    import re as _re
    log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "proxy_v54.log")
    if not os.path.exists(log_path):
        return "로그 파일 없음"
    try:
        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()[-500:]
        errors = [l.strip() for l in lines
                  if _re.search(r'ERROR|CRITICAL|Traceback|Exception:', l)][-100:]
        return "\n".join(errors) if errors else "최근 에러 없음"
    except Exception as e:
        return f"로그 조회 실패: {e}"


def _collect_backtest_summary() -> str:
    """백테스트 결과 요약 로드 (섹터별 최적 min_signal 힌트)."""
    summary_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_summary.json")
    if not os.path.exists(summary_path):
        return "백테스트 요약 없음"
    try:
        with open(summary_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        lines = [f"백테스트 기간: {data.get('period', '')}"]
        for sector, info in data.get("findings", {}).items():
            lines.append(
                f"- {sector}({info['symbol']}): 최적필터={info['best_period']}일 "
                f"수익률={info['best_return']}% 샤프={info['best_sharpe']} "
                f"승률={info['best_winrate']}% 권장min_signal={info['recommended_min_signal']} "
                f"({info['note']})"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"백테스트 요약 조회 실패: {e}"


def system_review(context_label: str = "정기점검") -> dict:
    """
    장 시작 전(08:00~08:10) / 장 마감 후(20:05~20:15) 호출.
    Gemma에게 전체 시스템 상태 + 보유종목 뉴스 보고 → 관리 지시 수신.
    완전 자율 모드: 지시를 즉시 실행 큐(_pending_manager_actions)에 적재.

    Args:
        context_label: "장전프리뷰" | "장후리뷰" | "정기점검"
    Returns:
        Gemma의 결정 dict (비어있으면 {})
    """
    global _last_system_review

    kst_now = datetime.datetime.now(pytz.timezone("Asia/Seoul"))

    with _lock:
        if _last_system_review:
            if (kst_now - _last_system_review).total_seconds() < 1800:
                return {}

    logger.info("🤖 Gemma 시스템 리뷰 시작 [%s]", context_label)

    portfolio    = _collect_portfolio()
    today_stats  = _collect_today_stats()
    errors       = _collect_error_summary()
    market       = _get_market_context()
    holdings_news = _collect_holdings_news()
    backtest_summary = _collect_backtest_summary()

    with _lock:
        strategy_txt = json.dumps(_current_strategy, ensure_ascii=False)

    # 타이밍별 역할 설명
    if context_label == "장전프리뷰":
        timing_guide = (
            "## 현재 상황: 장 시작 전 프리뷰\n"
            "오늘 장을 앞두고 전략을 점검하세요.\n"
            "- 보유종목 중 악재 뉴스가 있으면 alerts로 알려주세요.\n"
            "- 오늘의 시장 분위기(거시지표)에 맞게 strategy_update로 전략을 조정하세요.\n"
            "- 즉시 매도가 필요한 종목(심각한 악재)은 sell_triggers에 넣으세요.\n"
        )
    elif context_label == "장후리뷰":
        timing_guide = (
            "## 현재 상황: 장 마감 후 리뷰\n"
            "오늘 매매 결과를 분석하고 내일 전략을 수립하세요.\n"
            "- 승률/손익 기반으로 strategy_update를 통해 내일 전략을 조정하세요.\n"
            "- 보유종목 중 내일 위험한 종목이 있으면 alerts로 알려주세요.\n"
            "- 오늘 에러가 있었다면 간단히 alerts에 요약해주세요.\n"
        )
    else:
        timing_guide = "## 현재 상황: 정기점검\n시스템 전반을 점검하세요.\n"

    prompt = f"""당신은 한국 자동매매 시스템의 완전 자율 관리자 AI입니다.
지시는 즉시 자동 실행됩니다. 신중하게 판단하세요.

{timing_guide}

## 포트폴리오 현황
{portfolio}

## 당일 매매 통계
{today_stats}

## 거시지표 (환율/유가/금리/지수)
{market}

## 보유종목 최신 뉴스
{holdings_news}

## 백테스트 결과 (섹터별 최적 신호 임계값 참고)
{backtest_summary}

## 현재 전략
{strategy_txt}

## 최근 에러 로그 (최대 100건)
{errors}

## 사용 가능한 조치
1. strategy_update: daily_strategy.json 업데이트 (risk_level/min_signal_override/focus_sectors/max_holdings/notes)
2. alerts: 텔레그램 알림 메시지 배열 (사용자에게 보낼 한국어 문장)
3. sell_triggers: 즉시 매도할 종목코드 배열 (예: ["005930"])
4. param_adjust: 파라미터 조정 로그용

JSON만 반환 (조치 불필요 시 null/빈값):
{{
  "strategy_update": null,
  "alerts": [],
  "sell_triggers": [],
  "param_adjust": {{}},
  "notes": "판단 근거 한 줄"
}}"""

    response = _call_pc_director(prompt)

    with _lock:
        _last_system_review = kst_now

    if not response:
        return {}

    try:
        import re as _re
        m = _re.search(r'\{[\s\S]*\}', response)
        if m:
            decision = json.loads(m.group())
            logger.info("🤖 Gemma [%s] 지시 수신: %s", context_label, decision.get("notes", ""))
            with _lock:
                _pending_manager_actions.append(decision)
            return decision
    except Exception as e:
        logger.error("Gemma 리뷰 응답 파싱 실패: %s | 원문: %s", e, response[:200])
    return {}


def check_holdings_news(items: list) -> list:
    """
    보유 종목별 뉴스 + 가격 변동을 Gemma에게 일괄 판단 요청.
    가격 변동이 우선 기준, 뉴스는 보조.

    Args:
        items: [{"name": "삼성전기", "ticker": "009150",
                 "news": "제목1 / 제목2 / 제목3",
                 "pnl": -2.3, "price_change_5m": -1.1}, ...]
    Returns:
        악재 종목 list: [{"ticker": ..., "name": ..., "severity": "HIGH/MEDIUM",
                          "reason": "..."}]
    """
    if not items:
        return []

    lines = []
    for it in items:
        lines.append(
            f"- {it['name']}({it['ticker']}): "
            f"보유손익{it['pnl']:+.1f}% | 5분변동{it.get('price_change_5m', 0):+.2f}% | "
            f"뉴스: {it['news']}"
        )

    prompt = f"""보유 종목들의 가격 변동과 뉴스를 보고 실제 악재가 있는 종목만 골라주세요.

판단 기준:
1. 가격이 5분 내 -1% 이상 하락하고 뉴스에 악재가 있으면 HIGH
2. 가격 변동은 없지만 심각한 공시·실적 악화 뉴스면 MEDIUM
3. 뉴스가 단순 전망이거나 가격에 영향 없으면 SKIP

보유 종목:
{chr(10).join(lines)}

악재 종목만 JSON 배열로 반환. 없으면 빈 배열:
[{{"ticker":"코드","name":"종목명","severity":"HIGH or MEDIUM","reason":"한줄 이유"}}]"""

    response = _call_pc_director(prompt)
    if not response:
        return []
    try:
        import re as _re
        m = _re.search(r'\[[\s\S]*\]', response)
        if m:
            result = json.loads(m.group())
            return result if isinstance(result, list) else []
    except Exception as e:
        logger.debug("check_holdings_news 파싱 실패: %s", e)
    return []


def get_pending_actions() -> list:
    """auto_trader.py가 폴링하는 함수 — 실행 대기 관리자 지시 반환 후 큐 초기화."""
    with _lock:
        actions = list(_pending_manager_actions)
        _pending_manager_actions.clear()
    return actions


def _save_strategy():
    """전략을 JSON 파일로 저장."""
    try:
        with open(STRATEGY_PATH, "w", encoding="utf-8") as f:
            json.dump(_current_strategy, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error("전략 저장 실패: %s", e)


def _load_strategy():
    """저장된 전략 로드."""
    global _current_strategy
    if os.path.exists(STRATEGY_PATH):
        try:
            with open(STRATEGY_PATH, "r", encoding="utf-8") as f:
                _current_strategy = json.load(f)
            logger.info("저장된 전략 로드: %s", _current_strategy.get("date"))
        except Exception as e:
            logger.error("전략 로드 실패: %s", e)


def is_trading_hours() -> bool:
    """거래시간 확인 (KST 기준)."""
    kst = datetime.datetime.now(pytz.timezone("Asia/Seoul"))
    return 9 <= kst.hour < 16 and kst.weekday() < 5  # 평일 09:00~16:00


# ── 백그라운드 스케줄러 ──────────────────────────────────────────────────────

def director_scheduler():
    """
    백그라운드 스레드:
    - 09:00: 당일 전략 수립
    - 15:00: 저녁 분석 준비
    """
    import time as _time

    _load_strategy()
    logger.info("🎯 PC LLM 디렉터 스케줄러 시작")

    while True:
        try:
            kst_now = datetime.datetime.now(pytz.timezone("Asia/Seoul"))

            # 장 시작 전 (09:00 ~ 09:05) — 당일 전략 수립
            if is_trading_hours() and 9 <= kst_now.hour < 10 and kst_now.minute < 5:
                today = kst_now.date().isoformat()
                if _current_strategy.get("date") != today:
                    init_daily_strategy()

            # 장 시작 전 프리뷰 (평일 08:00~08:10)
            if (kst_now.weekday() < 5
                    and kst_now.hour == 8 and kst_now.minute < 10):
                today = kst_now.date().isoformat()
                if getattr(_last_system_review, 'date', lambda: None)() != kst_now.date() \
                        or _last_system_review is None:
                    system_review(context_label="장전프리뷰")

            # 장 마감 후 리뷰 (평일 20:05~20:15)
            if (kst_now.weekday() < 5
                    and kst_now.hour == 20 and 5 <= kst_now.minute < 15):
                system_review(context_label="장후리뷰")

            # 장 마감 전 준비 (15:30)
            if kst_now.hour == 15 and kst_now.minute == 30 and kst_now.weekday() < 5:
                logger.info("📊 저녁 분석 예약됨 (장 마감 후)")

            _time.sleep(60)
        except Exception:
            logger.exception("director_scheduler 오류")
            _time.sleep(60)


if __name__ == "__main__":
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
        handlers=[
            logging.FileHandler(os.path.join(os.path.dirname(__file__), "pc_director.log")),
            logging.StreamHandler()
        ]
    )

    if len(sys.argv) > 1 and sys.argv[1] == "test":
        # 테스트: PC LLM 당일 전략 수립
        init_daily_strategy()
        print(json.dumps(_current_strategy, ensure_ascii=False, indent=2))
    else:
        # 백그라운드 실행
        t = threading.Thread(target=director_scheduler, daemon=True)
        t.start()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("PC LLM 디렉터 종료")
