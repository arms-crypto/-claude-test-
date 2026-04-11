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
OLLAMA_PC    = "http://221.144.111.116:11434/api/chat"
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
    없거나 30일 초과면 백그라운드 학습을 트리거하고 기본값 반환.
    """
    p = _params.get(sector)
    if p and _is_fresh(p):
        return p

    reason = "없음" if not p else "30일 초과"
    logger.info("[sector_params] %s 파라미터 %s → 백그라운드 학습 트리거", sector, reason)
    _trigger(sector)
    return p or DEFAULT_PARAMS.copy()


def is_learning(sector: str) -> bool:
    """해당 업종이 현재 학습 중인지 확인."""
    with _lock:
        return sector in _learning


def all_params() -> dict:
    """전체 파라미터 사전 반환 (읽기 전용)."""
    return dict(_params)


# ── 내부 함수 ─────────────────────────────────────────────────────────────────

def _is_fresh(p: dict) -> bool:
    updated = p.get("updated_at")
    if not updated:
        return False
    try:
        dt = datetime.datetime.fromisoformat(updated)
        return (datetime.datetime.now() - dt).days < STALE_DAYS
    except Exception:
        return False


def _trigger(sector: str):
    """중복 방지 후 백그라운드 학습 스레드 시작."""
    with _lock:
        if sector in _learning:
            return
        _learning.add(sector)
    t = threading.Thread(target=_worker, args=(sector,), daemon=True, name=f"sector_learn_{sector}")
    t.start()


def _worker(sector: str):
    """백그라운드 학습 워커."""
    try:
        logger.info("[sector_params] ▶ 학습 시작: %s", sector)
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
    """PC → 로컬 순서로 Ollama 호출."""
    for url, model in [(OLLAMA_PC, "mistral-small3.1:24b"), (OLLAMA_LOCAL, "gemma3:4b")]:
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
            logger.warning("[sector_params] Ollama 호출 실패 (%s): %s", url, e)
    return ""


def _derive_one(sector: str) -> dict:
    """
    sector_signal.db 통계 → Ollama → 업종 파라미터 1개 도출.
    실패 시 DEFAULT_PARAMS 반환.
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
