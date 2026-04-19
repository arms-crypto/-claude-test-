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
    """간단한 시장 정보 조회."""
    try:
        from stock_data import get_naver_index_data
        kospi = get_naver_index_data("KOSPI")
        kosdaq = get_naver_index_data("KOSDAQ")
        return f"KOSPI: {kospi.get('price', '?')} ({kospi.get('change_pct', '?')}%)\nKOSDAQ: {kosdaq.get('price', '?')} ({kosdaq.get('change_pct', '?')}%)"
    except Exception as e:
        logger.debug("시장 정보 조회 실패: %s", e)
        return "시장 정보 조회 실패"


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

            # 장 시작 전 (09:00 ~ 09:05)
            if is_trading_hours() and 9 <= kst_now.hour < 10 and kst_now.minute < 5:
                today = kst_now.date().isoformat()
                if _current_strategy.get("date") != today:
                    init_daily_strategy()

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
