#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sector_params.py — 업종별 매매 파라미터 자동 관리

흐름:
  부팅 시   : load() → sector_params.json 로드
  매매/분석 시: get(sector) 호출
    → 파라미터 있고 30일 이내 → 즉시 반환
    → 없거나 30일 초과        → 백그라운드 학습 트리거 + 기본값 반환
  백그라운드 : sector_signal.db 데이터 부족 시 KIS 수집 → Ollama 분석 → JSON 저장
"""

import json, os, re, sqlite3, threading, logging, datetime, requests

logger = logging.getLogger("sector_params")

_BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
PARAMS_PATH  = os.path.join(_BASE_DIR, "sector_params.json")
DB_PATH      = os.path.join(_BASE_DIR, "mock_trading", "sector_signal.db")
OLLAMA_PC    = "http://221.144.111.116:8000/v1/chat/completions"
OLLAMA_LOCAL = "http://localhost:11434/api/chat"

STALE_DAYS   = 30   # 파라미터 신선도 기준 (일)
MIN_ROWS     = 50   # 학습 최소 데이터 행 수

DEFAULT_PARAMS = {
    "min_signal":   6,
    "require_both": False,
    "max_atr_pct":  8.0,
    "hold_days":    10,
    "note":         "기본값",
    "updated_at":   None,
}

_params:   dict       = {}      # 메모리 캐시
_learning: set        = set()   # 현재 학습 중인 업종
_lock:     threading.Lock = threading.Lock()


# ── 공개 API ──────────────────────────────────────────────────────────────────

def load():
    """서버 시작 시 1회 호출 — sector_params.json 로드."""
    global _params
    if os.path.exists(PARAMS_PATH):
        try:
            with open(PARAMS_PATH, encoding="utf-8") as f:
                _params = json.load(f)
            logger.info("[sector_params] JSON 로드 완료: %d개 업종", len(_params))
        except Exception as e:
            logger.warning("[sector_params] JSON 로드 실패: %s", e)
            _params = {}
    else:
        logger.info("[sector_params] sector_params.json 없음 — 첫 매매 시 자동 학습")


def get(sector: str) -> dict:
    """
    업종 파라미터 반환.
    없거나 30일 초과면 즉시 학습 트리거.

    새 종목(파라미터 없음): 보수적 기본값 + 즉시 학습
    기존 종목(30일 초과): 백그라운드 학습 + 기존값 계속 사용
    """
    p = _params.get(sector)
    is_new = not p
    is_stale = p and not _is_fresh(p)

    if p and _is_fresh(p):
        return p

    if is_new:
        # 새 종목: 보수적 기본값 + 즉시 학습
        logger.info("[sector_params] 🆕 %s 신규 종목 → 보수적 기본값(min_signal=7) + 즉시 학습", sector)
        _trigger(sector, is_urgent=True)
        conservative = DEFAULT_PARAMS.copy()
        conservative["min_signal"] = 7  # 신규: 신호 7/12 이상만 매수
        conservative["note"] = f"신규 종목 학습 중 — {get_timestamp()}"
        return conservative
    else:
        # 기존 종목 파라미터 만료: 백그라운드 학습
        logger.info("[sector_params] %s 파라미터 30일 초과 → 백그라운드 학습 트리거", sector)
        _trigger(sector, is_urgent=False)
        return p


def get_timestamp() -> str:
    """현재 시간 반환 (로그용)."""
    from datetime import datetime
    return datetime.now().strftime("%H:%M")


def is_learning(sector: str) -> bool:
    """해당 업종이 현재 학습 중인지 확인."""
    with _lock:
        return sector in _learning


def all_params() -> dict:
    """전체 파라미터 사전 반환 (읽기 전용)."""
    return dict(_params)


# ── 내부 함수 ─────────────────────────────────────────────────────────────────

def _is_fresh(p: dict) -> bool:
    """파라미터가 STALE_DAYS 이내에 갱신된 신선한 데이터인지 확인.

    Args:
        p: 파라미터 딕셔너리. 'updated_at' 키(ISO 형식 문자열)를 포함해야 한다.

    Returns:
        updated_at이 있고 현재로부터 STALE_DAYS일 미만이면 True, 그 외 False.
    """
    updated = p.get("updated_at")
    if not updated:
        return False
    try:
        dt = datetime.datetime.fromisoformat(updated)
        return (datetime.datetime.now() - dt).days < STALE_DAYS
    except Exception:
        return False


def _trigger(sector: str, is_urgent: bool = False):
    """중복 방지 후 백그라운드 학습 스레드 시작.

    이미 학습 중인 업종(_learning 집합)은 건너뛴다.
    _learning에 추가 후 daemon 스레드(_worker)를 시작하며,
    스레드 완료 시 _worker의 finally 블록에서 _learning에서 제거된다.

    Args:
        sector: 학습할 업종명 (예: '반도체', '방산').
        is_urgent: True면 새 종목 (빠른 학습 우선), False면 정기 갱신.
    """
    with _lock:
        if sector in _learning:
            return
        _learning.add(sector)

    priority_name = "🆕 신규" if is_urgent else "📊 정기"
    t = threading.Thread(
        target=_worker,
        args=(sector, is_urgent),
        daemon=True,
        name=f"sector_learn_{sector}_{priority_name}"
    )
    t.start()


def _worker(sector: str, is_urgent: bool = False):
    """백그라운드 학습 워커.

    업종별 신호 데이터를 수집하고 학습하여 파라미터를 도출한 후,
    이를 JSON 파일에 저장합니다.

    Args:
        sector: 학습할 업종명 (예: '반도체', '방산').
        is_urgent: True면 새 종목 (우선순위 높음), False면 정기 갱신.
    """
    try:
        priority_tag = "🆕 신규" if is_urgent else "📊 정기"
        logger.info("[sector_params] ▶ 학습 시작: %s [%s]", sector, priority_tag)
        from train_sector_kis import SECTOR_STOCKS, process_stock, learn_sector, init_db

        codes = SECTOR_STOCKS.get(sector)
        if not codes:
            logger.warning("[sector_params] 알 수 없는 업종: %s", sector)
            return

        # 1) DB 데이터 충분한지 확인
        init_db()
        con = sqlite3.connect(DB_PATH)
        cnt = con.execute(
            "SELECT COUNT(*) FROM sector_signals WHERE sector=?", (sector,)
        ).fetchone()[0]
        con.close()

        # 2) 데이터 부족 시 KIS 수집
        if cnt < MIN_ROWS:
            logger.info("[sector_params] %s 데이터 부족(%d건) → KIS 수집 시작", sector, cnt)
            import time as _time
            for code, name, cap_type in codes:
                try:
                    process_stock(sector, code, name, cap_type)
                    _time.sleep(1.0)
                except Exception as e:
                    logger.warning("[sector_params] KIS 수집 실패 %s(%s): %s", name, code, e)

        # 3) Ollama 정성 학습 (RAG 저장)
        learn_sector(sector, codes)

        # 4) 정량 파라미터 도출 → JSON 갱신
        p = _derive_one(sector)
        p["updated_at"] = datetime.datetime.now().isoformat(timespec="seconds")
        _params[sector] = p
        _save()
        logger.info("[sector_params] ✅ 학습 완료: %s → %s", sector, p)

    except Exception as e:
        logger.error("[sector_params] 학습 실패 %s: %s", sector, e, exc_info=True)
    finally:
        with _lock:
            _learning.discard(sector)


def _ask_ollama(prompt: str) -> str:
    """PC(LM Studio) → 로컬(Ollama) 순서로 LLM을 호출하여 응답을 반환한다."""
    endpoints = [
        (OLLAMA_PC,    "google_gemma-4-26b-a4b-it", "openai"),   # PC LM Studio
        (OLLAMA_LOCAL, "gemma3:4b",                  "ollama"),   # 로컬 Ollama
    ]
    for url, model, api_type in endpoints:
        try:
            if api_type == "openai":
                # LM Studio — OpenAI 호환 포맷
                payload = {
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.3,
                    "stream": False,
                }
                r = requests.post(url, json=payload, timeout=180,
                                  proxies={"http": None, "https": None})
                r.raise_for_status()
                return r.json()["choices"][0]["message"]["content"].strip()
            else:
                # 로컬 Ollama 포맷
                payload = {
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                    "options": {"temperature": 0.3},
                }
                r = requests.post(url, json=payload, timeout=180,
                                  proxies={"http": None, "https": None})
                r.raise_for_status()
                return r.json()["message"]["content"].strip()
        except Exception as e:
            logger.warning("[sector_params] LLM 호출 실패 (%s): %s", url, e)
    return ""


def _derive_one(sector: str) -> dict:
    """sector_signal.db 통계를 분석해 Ollama로 업종 파라미터 1개를 도출한다.

    DB에서 해당 업종의 신호수별 승률/수익률 통계를 집계하고,
    Ollama에 프롬프트로 전달해 최적 매매 파라미터 JSON을 반환받는다.

    Args:
        sector: 파라미터를 도출할 업종명 (예: '반도체', '방산').

    Returns:
        min_signal, require_both, max_atr_pct, hold_days, note 키를 포함한 딕셔너리.
        데이터 부족 또는 실패 시 DEFAULT_PARAMS 반환.
    """
    default = DEFAULT_PARAMS.copy()
    try:
        import pandas as pd
        con = sqlite3.connect(DB_PATH)
        rows = con.execute("""
            SELECT buy_count, next1_pct, next3_pct, next6_pct
            FROM sector_signals
            WHERE sector=? AND next3_pct IS NOT NULL
        """, (sector,)).fetchall()
        con.close()

        if not rows:
            return default

        df = pd.DataFrame(rows, columns=["buy_count", "n1", "n3", "n6"])

        stats_lines = []
        for sig in range(4, 13):
            sub = df[df["buy_count"] >= sig]["n3"].dropna()
            if len(sub) >= 5:
                wr  = (sub > 0).mean() * 100
                avg = sub.mean()
                stats_lines.append(f"  신호≥{sig}: {len(sub)}건 / 승률{wr:.0f}% / 평균수익{avg:+.1f}%")

        hold_lines = []
        for col, days in [("n1", "1개월"), ("n3", "3개월"), ("n6", "6개월")]:
            sub6 = df[df["buy_count"] >= 6][col].dropna()
            if len(sub6) >= 5:
                wr  = (sub6 > 0).mean() * 100
                avg = sub6.mean()
                hold_lines.append(f"  {days}: 승률{wr:.0f}% / 평균{avg:+.1f}%")

        if not stats_lines:
            return default

        prompt = f"""{sector} 업종 10년 데이터 분석 결과야.

[신호수별 3개월 후 승률]
{chr(10).join(stats_lines)}

[신호≥6 기준 보유기간별 승률]
{chr(10).join(hold_lines)}

이 데이터를 분석해서 {sector} 업종 최적 매매 파라미터를 JSON으로만 반환해줘.

반드시 아래 JSON 형식만 반환 (다른 텍스트 금지):
{{
  "min_signal": 6~12 사이 정수 (승률이 60% 이상 되는 최소 신호 수),
  "require_both": true/false (외국인+기관 동시 순매수 필수 여부),
  "max_atr_pct": 3.0~15.0 (허용 최대 일일변동폭 %),
  "hold_days": 5 또는 10 또는 20 (최적 보유 기간),
  "note": "한줄 근거"
}}"""

        raw = _ask_ollama(prompt)
        if not raw:
            return default

        m = re.search(r'\{[^{}]+\}', raw, re.DOTALL)
        if not m:
            return default

        p = json.loads(m.group())
        return {
            "min_signal":   max(6, min(12, int(p.get("min_signal", 6)))),
            "require_both": bool(p.get("require_both", False)),
            "max_atr_pct":  max(2.0, min(20.0, float(p.get("max_atr_pct", 8.0)))),
            "hold_days":    int(p.get("hold_days", 10)),
            "note":         str(p.get("note", ""))[:100],
        }

    except Exception as e:
        logger.warning("[sector_params] _derive_one 실패 %s: %s", sector, e)
        return default


def _save():
    """메모리 캐시를 sector_params.json 파일로 저장한다.

    PARAMS_PATH 경로에 UTF-8 인코딩으로 JSON 저장.
    실패 시 logger.error로 기록하고 조용히 무시한다.
    """
    try:
        with open(PARAMS_PATH, "w", encoding="utf-8") as f:
            json.dump(_params, f, ensure_ascii=False, indent=2)
        logger.info("[sector_params] JSON 저장: %d개 업종", len(_params))
    except Exception as e:
        logger.error("[sector_params] JSON 저장 실패: %s", e)


def _get_combo_reliability(combo: str) -> int:
    """신호 조합별 신뢰도 점수 (백테스팅 결과 기반).

    Args:
        combo: 신호 조합 문자열 (예: 'strong/strong/weak')

    Returns:
        신뢰도 점수 (0-100)
    """
    reliability_map = {
        "strong/strong/weak": 71,    # 가장 신뢰할만함
        "strong/strong/strong": 66,
        "strong/weak/strong": 66,
        "strong/weak/weak": 65,
        "weak/strong/weak": 66,
        "weak/weak/strong": 66,
        "weak/strong/strong": 57,    # 낮음
        "weak/weak/weak": 61,
    }
    return reliability_map.get(combo, 60)


def monthly_learn():
    """
    📚 월간 학습: PC의 min_signal 제안 → 거래 결과 → 지표 가중치 최적화

    학습 프로세스:
    1. pc_learning_history.json에서 PC 제안 읽기
    2. 각 제안의 실제 수익률 계산 (portfolio.db 매칭)
    3. 지표별 정확도 분석 (ADX/RSI/MACD)
    4. 타임프레임별 가중치 조정 (월/주/일)
    5. pc_indicator_weights.json 업데이트

    Returns:
        dict: 학습 결과 및 가중치 변경사항
    """
    learning_path = os.path.join(_BASE_DIR, "pc_learning_history.json")
    weights_path = os.path.join(_BASE_DIR, "pc_indicator_weights.json")

    if not os.path.exists(learning_path):
        return {"status": "no_learning_data", "updated": False}

    try:
        with open(learning_path, 'r', encoding='utf-8') as f:
            learning_data = json.load(f)
    except Exception as e:
        logger.warning("[sector_params] 학습데이터 로드 실패: %s", e)
        return {"status": "load_failed", "updated": False}

    if not learning_data:
        return {"status": "empty", "updated": False}

    # 현재 가중치 로드
    try:
        with open(weights_path, 'r', encoding='utf-8') as f:
            weights = json.load(f)
    except:
        logger.warning("[sector_params] 가중치 파일 없음 — 초기값 사용")
        weights = _init_default_weights()

    logger.info("[sector_params] 📚 월간 학습 시작: %d개 PC 제안 분석", len(learning_data))

    # 타임프레임별 지표 정확도 분석
    analysis = {
        "월봉": _analyze_timeframe_accuracy(learning_data, "월"),
        "주봉": _analyze_timeframe_accuracy(learning_data, "주"),
        "일봉": _analyze_timeframe_accuracy(learning_data, "일"),
    }

    logger.info("[sector_params] 📊 학습 분석 완료 — 가중치 업데이트 시작")

    # 가중치 업데이트 — 상관도 기반 조정
    # 규칙: correlation >= 0.6 → weight +0.1
    #        correlation 0.4~0.6 → 변화 없음
    #        correlation < 0.4 → weight -0.1
    # 범위: min 0.3, max 1.5
    tf_key_map = {"월봉": "월봉", "주봉": "주봉", "일봉": "일봉"}
    changes = {}
    updated = False

    for tf_label, acc_data in analysis.items():
        sample_count = acc_data.get("sample_count", 0)
        if sample_count < 3:
            # 샘플 부족 — 업데이트 스킵
            logger.info("[sector_params] %s 샘플 부족(%d) — 가중치 업데이트 스킵", tf_label, sample_count)
            continue

        tf_weights = weights.get(tf_label, {})
        tf_changes = {}

        for indicator in ("ADX", "RSI", "MACD"):
            ind_data = acc_data.get(indicator, {})
            corr = ind_data.get("correlation_with_return", 0)

            current_w_entry = tf_weights.get(indicator, {})
            # 구조가 float인 경우(초기값)와 dict인 경우 모두 처리
            if isinstance(current_w_entry, dict):
                current_w = current_w_entry.get("weight", 1.0)
            else:
                current_w = float(current_w_entry)

            # 상관도 기반 조정
            if corr >= 0.6:
                delta = +0.1
            elif corr < 0.4:
                delta = -0.1
            else:
                delta = 0.0

            new_w = round(max(0.3, min(1.5, current_w + delta)), 1)

            if new_w != current_w:
                tf_changes[indicator] = {"before": current_w, "after": new_w, "corr": corr}
                updated = True
                logger.info(
                    "[sector_params] 가중치 조정: %s %s %.1f→%.1f (상관도%.2f)",
                    tf_label, indicator, current_w, new_w, corr
                )

            # weights 딕셔너리에 반영 (dict 구조 유지)
            if isinstance(tf_weights.get(indicator), dict):
                weights[tf_label][indicator]["weight"] = new_w
                weights[tf_label][indicator]["sample_count"] = (
                    weights[tf_label][indicator].get("sample_count", 0) + sample_count
                )
            else:
                # _init_default_weights()의 단순 float 구조 → dict로 업그레이드
                weights[tf_label][indicator] = {
                    "weight": new_w,
                    "win_rate": 0.0,
                    "avg_return": 0.0,
                    "sample_count": sample_count
                }

        if tf_changes:
            changes[tf_label] = tf_changes

    # 파일 저장
    if updated:
        weights["last_updated"] = datetime.datetime.now().strftime("%Y-%m-%d")
        try:
            with open(weights_path, "w", encoding="utf-8") as f:
                json.dump(weights, f, ensure_ascii=False, indent=2)
            logger.info("[sector_params] ✅ pc_indicator_weights.json 업데이트 완료")
        except Exception as e:
            logger.error("[sector_params] 가중치 파일 저장 실패: %s", e)
            updated = False
    else:
        logger.info("[sector_params] 가중치 변화 없음 (상관도 0.4~0.6 범위 내)")

    return {
        "status": "completed",
        "samples_analyzed": len(learning_data),
        "analysis": analysis,
        "weights": weights,
        "changes": changes,
        "updated": updated
    }


def _analyze_timeframe_accuracy(learning_data: list, timeframe: str) -> dict:
    """
    타임프레임별 지표 정확도 분석.

    PC LLM이 제안한 신호 변화의 거래 결과를 분석해서
    각 지표(ADX/RSI/MACD)와 실제 수익률의 상관도를 계산한다.

    Args:
        learning_data: pc_learning_history.json 데이터
        timeframe: "월", "주", "일"

    Returns:
        dict: 각 지표의 평균 강도, 상관도, 가중치 제안
    """
    try:
        con = sqlite3.connect(os.path.join(_BASE_DIR, "mock_trading", "portfolio.db"))
        cursor = con.cursor()

        # 1. 학습데이터에서 매칭 가능한 거래 찾기
        matched_results = []

        for entry in learning_data:
            code = entry.get("code")
            date_str = entry.get("date")  # "2026-04-14" 형식

            if not code or not date_str:
                continue

            aux_strengths = entry.get("auxiliary_strengths", {})
            timeframe_data = aux_strengths.get(timeframe, {})

            if not timeframe_data:
                continue

            # 해당 종목, 해당 날짜의 BUY 거래 찾기
            # (created_at LIKE '2026-04-14%' AND action='BUY')
            buy_trades = cursor.execute("""
                SELECT id, pnl FROM trades
                WHERE ticker = ? AND action = 'BUY'
                AND created_at LIKE ?
                ORDER BY created_at
            """, (code, f"{date_str}%")).fetchall()

            if not buy_trades:
                continue

            # BUY 거래들의 평균 pnl 계산
            # (같은 날짜에 여러 BUY가 있으면 평균)
            pnls = [t[1] for t in buy_trades if t[1] is not None]
            if not pnls:
                continue

            avg_pnl = sum(pnls) / len(pnls)

            # 지표 강도값 추출
            adx_strength = timeframe_data.get("adx", 0)
            rsi_strength = timeframe_data.get("rsi", 0)
            macd_strength = timeframe_data.get("macd", 0)

            matched_results.append({
                "code": code,
                "date": date_str,
                "adx_strength": adx_strength,
                "rsi_strength": rsi_strength,
                "macd_strength": macd_strength,
                "pnl": avg_pnl
            })

        con.close()

        if not matched_results:
            # 데이터 없으면 초기값 반환
            return {
                "sample_count": 0,
                "ADX": {"avg_strength": 0, "correlation_with_return": 0, "suggested_weight": 1.0},
                "RSI": {"avg_strength": 0, "correlation_with_return": 0, "suggested_weight": 0.7},
                "MACD": {"avg_strength": 0, "correlation_with_return": 0, "suggested_weight": 0.8}
            }

        # 2. 지표별 상관도 계산 (Pearson 상관계수)
        try:
            import statistics

            # 지표 강도와 실제 수익률의 상관도 계산
            def pearson_correlation(x_list, y_list):
                """Pearson 상관계수 계산."""
                if len(x_list) < 2 or len(y_list) < 2:
                    return 0.0

                n = len(x_list)
                mean_x = statistics.mean(x_list)
                mean_y = statistics.mean(y_list)

                numerator = sum((x_list[i] - mean_x) * (y_list[i] - mean_y) for i in range(n))
                denom_x = sum((x - mean_x) ** 2 for x in x_list)
                denom_y = sum((y - mean_y) ** 2 for y in y_list)

                if denom_x == 0 or denom_y == 0:
                    return 0.0

                return numerator / (denom_x * denom_y) ** 0.5

            adx_strengths = [r["adx_strength"] for r in matched_results]
            rsi_strengths = [r["rsi_strength"] for r in matched_results]
            macd_strengths = [r["macd_strength"] for r in matched_results]
            pnls = [r["pnl"] for r in matched_results]

            adx_corr = pearson_correlation(adx_strengths, pnls)
            rsi_corr = pearson_correlation(rsi_strengths, pnls)
            macd_corr = pearson_correlation(macd_strengths, pnls)

            # 상관도를 0~1 범위로 정규화 (절대값)
            adx_corr = abs(adx_corr)
            rsi_corr = abs(rsi_corr)
            macd_corr = abs(macd_corr)

            # 3. 상관도 기반 가중치 제안 (높은 상관 → 높은 가중치)
            # 정규화: 상관도 0.5 이상만 신뢰
            def strength_to_weight(corr):
                if corr >= 0.6:
                    return 1.2
                elif corr >= 0.5:
                    return 1.0
                elif corr >= 0.4:
                    return 0.8
                else:
                    return 0.5

            adx_weight = strength_to_weight(adx_corr)
            rsi_weight = strength_to_weight(rsi_corr)
            macd_weight = strength_to_weight(macd_corr)

            return {
                "sample_count": len(matched_results),
                "ADX": {
                    "avg_strength": round(statistics.mean(adx_strengths), 2) if adx_strengths else 0,
                    "correlation_with_return": round(adx_corr, 2),
                    "suggested_weight": round(adx_weight, 1)
                },
                "RSI": {
                    "avg_strength": round(statistics.mean(rsi_strengths), 2) if rsi_strengths else 0,
                    "correlation_with_return": round(rsi_corr, 2),
                    "suggested_weight": round(rsi_weight, 1)
                },
                "MACD": {
                    "avg_strength": round(statistics.mean(macd_strengths), 2) if macd_strengths else 0,
                    "correlation_with_return": round(macd_corr, 2),
                    "suggested_weight": round(macd_weight, 1)
                }
            }

        except Exception as e:
            logger.warning("[sector_params] 상관도 계산 실패 (%s): %s", timeframe, e)
            return {
                "sample_count": len(matched_results),
                "ADX": {"avg_strength": 0, "correlation_with_return": 0, "suggested_weight": 1.0},
                "RSI": {"avg_strength": 0, "correlation_with_return": 0, "suggested_weight": 0.7},
                "MACD": {"avg_strength": 0, "correlation_with_return": 0, "suggested_weight": 0.8}
            }

    except Exception as e:
        logger.error("[sector_params] _analyze_timeframe_accuracy 실패: %s", e)
        return {
            "sample_count": 0,
            "ADX": {"avg_strength": 0, "correlation_with_return": 0, "suggested_weight": 1.0},
            "RSI": {"avg_strength": 0, "correlation_with_return": 0, "suggested_weight": 0.7},
            "MACD": {"avg_strength": 0, "correlation_with_return": 0, "suggested_weight": 0.8}
        }


def _init_default_weights() -> dict:
    """기본 가중치 초기화."""
    return {
        "월봉": {"ADX": 1.0, "RSI": 0.7, "MACD": 0.8},
        "주봉": {"ADX": 0.9, "RSI": 0.6, "MACD": 0.7},
        "일봉": {"ADX": 0.3, "RSI": 1.0, "MACD": 0.8}
    }


def monthly_review():
    """
    매월 리뷰: PC LLM의 학습데이터 → sector_params 반영.

    pc_learning_history.json을 분석해서:
    1. 업종별 PC min_signal 제안 통계
    2. 신호 조합 신뢰도 검증
    3. 신뢰도 기반 필터링 적용
    4. sector_params.json에 반영 여부 결정

    Returns:
        dict: 리뷰 결과 및 적용 현황
    """
    learning_path = os.path.join(_BASE_DIR, "pc_learning_history.json")
    if not os.path.exists(learning_path):
        logger.info("[sector_params] 학습데이터 없음 — 리뷰 스킵")
        return {"status": "no_data", "applied": 0}

    try:
        with open(learning_path, 'r', encoding='utf-8') as f:
            learning_data = json.load(f)
    except Exception as e:
        logger.warning("[sector_params] 학습데이터 로드 실패: %s", e)
        return {"status": "load_failed", "applied": 0}

    if not learning_data:
        return {"status": "empty", "applied": 0}

    # 종목코드 → 업종명 매핑 (빠른 조회용)
    from train_sector_kis import SECTOR_STOCKS
    code_to_sector = {}
    for sector, codes in SECTOR_STOCKS.items():
        for code, name, cap_type in codes:
            code_to_sector[code] = sector

    # 업종별 PC min_signal 제안 & 신뢰도 집계 (신호 조합 + 보조 지표 + PC 신뢰도)
    sector_stats = {}
    for entry in learning_data:
        code = entry.get("code")
        sector = code_to_sector.get(code)
        if not sector:
            continue

        min_sig = entry.get("pc_min_signal_suggestion")
        if min_sig is None:
            continue

        # 1. 신호 조합 신뢰도 (signal_combo 기반)
        combo = entry.get("signal_combo", "unknown")
        combo_reliability = _get_combo_reliability(combo)

        # 2. PC/보조 지표 신뢰도 (직접 계산된 점수)
        pc_reliability = entry.get("reliability_score", 50)

        # 3. 두 신뢰도 종합 (신호 조합 40% + PC 계산 60%)
        combined_reliability = (combo_reliability * 0.4) + (pc_reliability * 0.6)

        if sector not in sector_stats:
            sector_stats[sector] = {
                "suggestions": [],
                "reliabilities": [],
                "combos": [],
                "pc_scores": [],  # PC 신뢰도 점수 추적
                "combined": []     # 종합 신뢰도
            }

        sector_stats[sector]["suggestions"].append(min_sig)
        sector_stats[sector]["reliabilities"].append(combo_reliability)
        sector_stats[sector]["combos"].append(combo)
        sector_stats[sector]["pc_scores"].append(pc_reliability)
        sector_stats[sector]["combined"].append(combined_reliability)

    # 업종별 통계 분석 & 적용
    applied_count = 0
    review_report = {
        "status": "completed",
        "reviewed_sectors": {},
        "applied": 0,
        "reliability_filters": {
            "high": 0,      # 신뢰도 > 70점 (즉시 적용)
            "medium": 0,    # 신뢰도 60-70점 (신중 적용)
            "low": 0        # 신뢰도 < 60점 (매우 신중)
        }
    }

    import statistics

    for sector, stats in sector_stats.items():
        if len(stats["suggestions"]) < 3:  # 최소 3개 데이터 필요
            continue

        avg_suggestion = statistics.mean(stats["suggestions"])
        # 신뢰도: 신호 조합 + PC 계산 + 보조 지표 종합
        avg_combo_reliability = statistics.mean(stats["reliabilities"])
        avg_pc_reliability = statistics.mean(stats["pc_scores"])
        avg_reliability = statistics.mean(stats["combined"])  # 종합 신뢰도 사용!

        suggested_min_signal = int(round(avg_suggestion))
        suggested_min_signal = max(4, min(7, suggested_min_signal))  # 범위: 4-7

        current = _params.get(sector, DEFAULT_PARAMS.copy())
        current_min_signal = current.get("min_signal", 6)
        difference = suggested_min_signal - current_min_signal

        review_report["reviewed_sectors"][sector] = {
            "sample_count": len(stats["suggestions"]),
            "avg_suggestion": round(avg_suggestion, 2),
            "avg_reliability": round(avg_reliability, 1),  # 종합 신뢰도
            "reliability_breakdown": {
                "signal_combo": round(avg_combo_reliability, 1),  # 신호 조합
                "pc_calculated": round(avg_pc_reliability, 1),     # PC 계산
                "combined": round(avg_reliability, 1)              # 종합
            },
            "suggested_min_signal": suggested_min_signal,
            "current_min_signal": current_min_signal,
            "difference": difference,
            "applied": False,
            "reason": ""
        }

        # 신뢰도 기반 적용 규칙
        should_apply = False
        reason = ""

        if avg_reliability > 70:
            # 높은 신뢰도: 변화량 ≥1 이면 적용
            if abs(difference) >= 1:
                should_apply = True
                reason = "높은 신뢰도(>70) → 즉시 적용"
                review_report["reliability_filters"]["high"] += 1
        elif avg_reliability >= 60:
            # 중간 신뢰도: 변화량 ≥1.5 (보수적)
            if abs(difference) >= 2:
                should_apply = True
                reason = "중간 신뢰도(60-70) → 신중 적용"
                review_report["reliability_filters"]["medium"] += 1
        else:
            # 낮은 신뢰도: 변화량 ≥2 (매우 보수적)
            if abs(difference) >= 2.5:
                should_apply = True
                reason = "낮은 신뢰도(<60) → 매우 신중 적용"
                review_report["reliability_filters"]["low"] += 1
            else:
                reason = "신뢰도 부족 → 누적 대기"

        if should_apply:
            current["min_signal"] = suggested_min_signal
            current["note"] = f"PC학습 {suggested_min_signal} (신뢰도{avg_reliability:.0f}점, {len(stats['suggestions'])}건) — {datetime.datetime.now().strftime('%Y-%m-%d')}"
            current["updated_at"] = datetime.datetime.now().isoformat(timespec="seconds")
            _params[sector] = current
            applied_count += 1

            review_report["reviewed_sectors"][sector]["applied"] = True
            review_report["reviewed_sectors"][sector]["reason"] = reason

            logger.info(
                "[sector_params] ✅ 적용: %s min_signal %d→%d (신뢰도%.0f점)",
                sector, current_min_signal, suggested_min_signal, avg_reliability
            )
        else:
            review_report["reviewed_sectors"][sector]["reason"] = reason
            logger.info(
                "[sector_params] ⏳ 대기: %s 신뢰도%.0f점, 변화량%+d — %s",
                sector, avg_reliability, difference, reason
            )

    if applied_count > 0:
        _save()
        logger.info("[sector_params] 📊 월간 리뷰 완료: %d개 업종 적용", applied_count)

    review_report["applied"] = applied_count
    return review_report
