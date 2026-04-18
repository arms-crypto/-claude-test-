#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
learn_chart_method.py — Ollama 차트 기법 자율 학습
signal_history DB의 상승/하락 구간 사례를 Ollama에게 보여주고
스스로 기법을 발견하게 한 뒤 chart_method_memory RAG에 저장.

학습 질문:
  - 월봉에서 상승 직전 공통 특징은?
  - 주봉에서 하락 직전 공통 특징은?
  - 일목균형이 진짜 의미있는 구간은?
  - 보조차트(RSI/MACD/ADX)가 핵심 신호가 되는 순간은?
  - 업종별 패턴 차이는?
"""

import os
import sys
import sqlite3
import hashlib
import logging
import requests
import json

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("learn_chart_method.log", encoding="utf-8"),
    ]
)
logger = logging.getLogger("learn_chart_method")

DB_PATH    = os.path.join(os.path.dirname(__file__), "mock_trading", "signal_history.db")
OLLAMA_URL = "http://221.144.111.116:11434/api/chat"  # PC Ollama
LOCAL_URL  = "http://localhost:11434/api/chat"         # 로컬 폴백
EMBED_URL  = "http://localhost:11434/api/embeddings"
EMBED_MODEL = "nomic-embed-text"
CHROMA_DIR = os.path.join(os.path.dirname(__file__), "rag_data")

# 업종 분류
SECTOR_MAP = {
    "005930": "반도체", "000660": "반도체",
    "005380": "자동차", "000270": "자동차",
    "068270": "바이오", "207940": "바이오",
    "035720": "IT플랫폼", "035420": "IT플랫폼",
    "105560": "금융", "055550": "금융",
}


# ── Ollama 호출 ──────────────────────────────────────────────────────────────

def _ask_ollama(prompt: str) -> str:
    """PC Ollama → 로컬 폴백."""
    for url in [OLLAMA_URL, LOCAL_URL]:
        try:
            r = requests.post(url, json={
                "model": "google_gemma-4-26b-a4b-it" if "11434" in url and "111.116" in url else "gemma3:4b",
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "options": {"temperature": 0.3}
            }, timeout=120, proxies={"http": None, "https": None})
            r.raise_for_status()
            return r.json()["message"]["content"].strip()
        except Exception as e:
            logger.warning("Ollama 호출 실패 (%s): %s", url, e)
    return ""


# ── DB 사례 추출 ─────────────────────────────────────────────────────────────

def fetch_cases(pattern: str, limit: int = 30) -> list:
    """signal_history에서 특정 패턴 사례 추출."""
    with sqlite3.connect(DB_PATH) as con:
        rows = con.execute("""
            SELECT code, name, date,
                   m_price_vs_kijun, m_price_vs_span1, m_price_vs_span2,
                   m_kijun_slope, m_rsi, m_macd, m_adx,
                   w_price_vs_kijun, w_price_vs_span1, w_price_vs_span2,
                   w_kijun_slope, w_rsi, w_macd, w_adx,
                   d_price_vs_kijun, d_price_vs_span1, d_price_vs_span2,
                   d_kijun_slope, d_rsi, d_macd, d_adx, d_ma_align,
                   peak_day, peak_pct, trough_day, trough_pct, pattern
            FROM signal_history
            WHERE pattern = ?
            ORDER BY RANDOM()
            LIMIT ?
        """, (pattern, limit)).fetchall()
    return rows


def _row_to_text(row) -> str:
    """DB row → 자연어 설명."""
    def pos(v): return "위" if v == 1 else ("아래" if v == -1 else "근접")
    def slp(v): return "↑" if v == 1 else ("↓" if v == -1 else "→")
    def ma(v):  return "정배열" if v == 1 else ("역배열" if v == -1 else "중립")

    (code, name, date,
     m_kijun, m_sp1, m_sp2, m_slope, m_rsi, m_macd, m_adx,
     w_kijun, w_sp1, w_sp2, w_slope, w_rsi, w_macd, w_adx,
     d_kijun, d_sp1, d_sp2, d_slope, d_rsi, d_macd, d_adx, d_ma,
     peak_day, peak_pct, trough_day, trough_pct, pattern) = row

    return (
        f"[{date} {name}({code})]\n"
        f"  월봉: 가격-기준선{pos(m_kijun)} 스팬1{pos(m_sp1)} 스팬2{pos(m_sp2)} "
        f"기준선{slp(m_slope)} RSI:{m_rsi} MACD:{'+' if m_macd and m_macd>0 else '-'} ADX:{m_adx}\n"
        f"  주봉: 가격-기준선{pos(w_kijun)} 스팬1{pos(w_sp1)} 스팬2{pos(w_sp2)} "
        f"기준선{slp(w_slope)} RSI:{w_rsi} MACD:{'+' if w_macd and w_macd>0 else '-'} ADX:{w_adx}\n"
        f"  일봉: 가격-기준선{pos(d_kijun)} 스팬1{pos(d_sp1)} 스팬2{pos(d_sp2)} "
        f"기준선{slp(d_slope)} RSI:{d_rsi} MACD:{'+' if d_macd and d_macd>0 else '-'} ADX:{d_adx} MA:{ma(d_ma)}\n"
        f"  결과: {pattern} 고점{peak_day}일후+{peak_pct}% / 저점{trough_day}일후{trough_pct}%"
    )


# ── RAG 저장 ─────────────────────────────────────────────────────────────────

def _embed(text: str) -> list:
    try:
        r = requests.post(EMBED_URL,
                          json={"model": EMBED_MODEL, "prompt": text[:2000]},
                          timeout=30, proxies={"http": None, "https": None})
        return r.json().get("embedding", [])
    except Exception:
        return []


def store_method(insight: str, category: str, sector: str = "전체"):
    """발견된 기법을 chart_method_memory RAG에 저장."""
    try:
        import chromadb
        client = chromadb.PersistentClient(path=CHROMA_DIR)
        col = client.get_or_create_collection("chart_method_memory")

        text = f"[기법/{category}/{sector}]\n{insight}"
        doc_id = hashlib.md5(f"{category}_{sector}_{insight[:50]}".encode()).hexdigest()

        emb = _embed(text)
        if not emb:
            logger.warning("임베딩 실패: %s", text[:50])
            return False

        try:
            col.delete(ids=[doc_id])
        except Exception:
            pass

        col.add(ids=[doc_id], embeddings=[emb],
                documents=[text],
                metadatas=[{"category": category, "sector": sector,
                            "trust": 1, "applied": 0, "correct": 0}])
        logger.info("기법 저장: [%s/%s] %s...", category, sector, insight[:60])
        return True
    except Exception as e:
        logger.error("기법 저장 실패: %s", e)
        return False


# ── 학습 질문 세트 ────────────────────────────────────────────────────────────

QUESTIONS = [
    # (카테고리, 패턴, 질문 템플릿)
    (
        "월봉_상승기법",
        ["UP_FIRST", "BREAKOUT"],
        """아래는 실제 주식 차트에서 상승이 시작되기 직전의 데이터야.
월봉/주봉/일봉의 가격-일목균형 라인 위치, RSI, MACD, ADX 값이 포함돼 있어.

{cases}

이 사례들을 분석해서:
1. 월봉에서 상승 직전 공통적으로 나타나는 핵심 특징 2~3가지
2. 일목균형 라인(기준선/선행스팬2)과 가격의 위치 관계에서 중요한 포인트 (이 차트에는 선행스팬1이 없으므로 기준선·선행스팬2·후행스팬만 언급할 것)
3. 보조지표(RSI/MACD/ADX) 중 월봉에서 진짜 의미있는 신호

한국어로 간결하게 답해줘. 트레이더가 바로 쓸 수 있는 실용적인 기법으로."""
    ),
    (
        "주봉_상승기법",
        ["UP_FIRST", "BREAKOUT"],
        """아래는 실제 주식 상승 직전 데이터야.

{cases}

주봉 관점에서:
1. 상승 직전 주봉에서 공통적으로 나타나는 핵심 특징 2~3가지
2. 주봉 기준선/스팬 위치와 가격의 관계에서 중요한 순간
3. 주봉에서 진짜 의미있는 거래량 패턴

한국어로 간결하게. 실용적인 기법으로."""
    ),
    (
        "월봉_하락기법",
        ["REVERSAL", "DOWN_FIRST"],
        """아래는 실제 주식 하락 직전 데이터야.

{cases}

1. 월봉에서 하락 전환 직전 공통 특징 2~3가지
2. 일목균형 라인에서 위험 신호가 되는 위치 관계
3. 매도/관망 타이밍을 알려주는 핵심 지표

한국어로 간결하게. 실용적인 기법으로."""
    ),
    (
        "주봉_하락기법",
        ["REVERSAL", "DOWN_FIRST"],
        """아래는 실제 주식 하락 직전 데이터야.

{cases}

주봉 관점에서:
1. 하락 전환 직전 주봉 공통 특징 2~3가지
2. 주봉 기준선/스팬에서 이탈 패턴
3. 주봉 RSI/MACD 경고 신호

한국어로 간결하게. 실용적인 기법으로."""
    ),
    (
        "일목균형_핵심구간",
        ["UP_FIRST", "BREAKOUT", "REVERSAL"],
        """아래는 상승과 하락이 모두 포함된 실제 차트 데이터야.

{cases}

일목균형표(기준선/선행스팬2/후행스팬) 관점에서 — 이 차트에는 선행스팬1이 없음, 절대 언급 금지:
1. 진짜 의미있는 신호가 나오는 구간 (월봉? 주봉? 어느 타임프레임?)
2. 가격과 기준선의 관계에서 가장 중요한 순간
3. 선행스팬2 위치가 실제로 중요한 경우와 무의미한 경우

한국어로 간결하게."""
    ),
    (
        "보조차트_핵심신호",
        ["UP_FIRST", "BREAKOUT", "REVERSAL", "DOWN_FIRST"],
        """아래는 다양한 구간의 실제 차트 데이터야.

{cases}

RSI/MACD/ADX 보조지표 관점에서:
1. 각 지표가 진짜 유효한 신호가 되는 조건 (단독? 조합?)
2. 월봉/주봉/일봉 중 각 지표가 가장 의미있는 타임프레임
3. 거짓 신호를 걸러내는 방법

한국어로 간결하게."""
    ),
    (
        "타임프레임_정렬",
        ["UP_FIRST", "BREAKOUT"],
        """아래는 강한 상승이 나온 사례들이야.

{cases}

월봉/주봉/일봉이 모두 같은 방향일 때와 엇갈릴 때의 차이:
1. 타임프레임 정렬이 완벽할 때 vs 부분 정렬일 때 수익률 차이
2. 어느 타임프레임 정렬이 가장 중요한가
3. 타임프레임 충돌 시 어떻게 판단해야 하는가

한국어로 간결하게. 실제 매매에 바로 쓸 수 있는 기법으로."""
    ),
]


# ── 메인 학습 루프 ────────────────────────────────────────────────────────────

def _start_keepalive():
    """4분 30초마다 /ping_sleep_timer 호출 → PC 절전 방지."""
    import threading, requests as _req
    def _loop():
        while True:
            time.sleep(270)
            try:
                _req.get("http://localhost:11435/ping_sleep_timer", timeout=5)
                logger.info("[킵얼라이브] 슬립 타이머 리셋")
            except Exception:
                pass
    threading.Thread(target=_loop, daemon=True).start()


def learn():
    logger.info("=" * 60)
    logger.info("Ollama 차트 기법 자율 학습 시작")
    logger.info("=" * 60)

    # PC 절전 방지
    try:
        import requests as _req
        _req.get("http://localhost:11435/ping_sleep_timer", timeout=5)
    except Exception:
        pass
    _start_keepalive()

    # DB 종목 목록 확인
    with sqlite3.connect(DB_PATH) as con:
        codes = [r[0] for r in con.execute(
            "SELECT DISTINCT code FROM signal_history").fetchall()]

    if not codes:
        logger.error("signal_history DB가 비어있음. build_signal_history.py 먼저 실행하세요.")
        sys.exit(1)

    logger.info("학습 데이터 종목: %s", codes)

    total_stored = 0

    for category, patterns, question_tpl in QUESTIONS:
        logger.info("\n--- %s 학습 중 ---", category)

        # 해당 패턴 사례 수집
        all_cases = []
        for p in patterns:
            all_cases += fetch_cases(p, limit=15)

        if not all_cases:
            logger.warning("%s 사례 없음 스킵", category)
            continue

        # 사례 텍스트 변환
        case_texts = [_row_to_text(r) for r in all_cases[:20]]
        cases_str = "\n\n".join(case_texts)

        prompt = question_tpl.format(cases=cases_str)

        logger.info("Ollama 질문 중... (%d 사례)", len(all_cases[:20]))
        insight = _ask_ollama(prompt)

        if not insight:
            logger.warning("Ollama 응답 없음: %s", category)
            continue

        logger.info("발견된 기법:\n%s\n", insight)

        # RAG 저장
        if store_method(insight, category, "전체"):
            total_stored += 1

        # 업종별 추가 학습 (데이터 충분하면)
        for code in codes:
            sector = SECTOR_MAP.get(code)
            if not sector:
                continue

            with sqlite3.connect(DB_PATH) as con:
                cnt = con.execute(
                    "SELECT COUNT(*) FROM signal_history WHERE code=? AND pattern IN ({})".format(
                        ",".join("?" * len(patterns))),
                    [code] + patterns).fetchone()[0]

            if cnt < 50:
                continue

            # 업종별 사례
            sector_cases = []
            for p in patterns:
                rows = fetch_cases(p, limit=10)
                rows = [r for r in rows if r[0] == code]
                sector_cases += rows

            if len(sector_cases) < 10:
                continue

            name = sector_cases[0][1] if sector_cases else code
            sector_case_texts = [_row_to_text(r) for r in sector_cases[:15]]
            sector_prompt = question_tpl.format(cases="\n\n".join(sector_case_texts))
            sector_prompt += f"\n\n특히 {sector} 업종({name}) 관점에서 추가 특징이 있으면 말해줘."

            logger.info("  업종별 학습: %s(%s)", sector, code)
            sector_insight = _ask_ollama(sector_prompt)

            if sector_insight and len(sector_insight) > 50:
                if store_method(sector_insight, category, sector):
                    total_stored += 1

    # 최종 종합 기법 추출
    logger.info("\n--- 종합 매매 기법 추출 ---")
    all_sample = (fetch_cases("UP_FIRST", 10) + fetch_cases("BREAKOUT", 10) +
                  fetch_cases("REVERSAL", 10) + fetch_cases("DOWN_FIRST", 10))

    if all_sample:
        final_prompt = f"""아래는 상승/하락 다양한 구간의 실제 차트 데이터야.

{chr(10).join(_row_to_text(r) for r in all_sample)}

지금까지 분석한 내용을 바탕으로:
1. 매수 진입 최적 조건 (월봉/주봉/일봉 각각의 핵심 체크포인트)
2. 매도/익절 신호 (어느 타임프레임에서 먼저 이탈하는가)
3. 절대 진입하면 안 되는 위험 패턴
4. 단타 vs 스윙 구분 기준

트레이더가 실제 매매에 바로 쓸 수 있게 간결하고 명확하게."""

        final_insight = _ask_ollama(final_prompt)
        if final_insight:
            logger.info("종합 기법:\n%s", final_insight)
            store_method(final_insight, "종합_매매기법", "전체")
            total_stored += 1

    logger.info("\n" + "=" * 60)
    logger.info("학습 완료: 총 %d개 기법 RAG 저장", total_stored)
    logger.info("=" * 60)

    # RAG 상태 출력
    try:
        import chromadb
        client = chromadb.PersistentClient(path=CHROMA_DIR)
        col = client.get_or_create_collection("chart_method_memory")
        logger.info("chart_method_memory: %d건", col.count())
    except Exception:
        pass


def update_method_trust(category: str, correct: bool):
    """
    매매 결과로 기법 신뢰도 업데이트.
    correct=True → 기법이 맞았음 / False → 틀렸음
    sell_mock/buy_mock 완료 후 호출.
    """
    try:
        import chromadb
        client = chromadb.PersistentClient(path=CHROMA_DIR)
        col = client.get_or_create_collection("chart_method_memory")
        if col.count() == 0:
            return

        # 해당 카테고리 기법 조회
        emb = _embed(category)
        if not emb:
            return
        r = col.query(query_embeddings=[emb], n_results=3)
        for doc_id, meta, doc in zip(
                r["ids"][0], r["metadatas"][0], r["documents"][0]):
            trust   = meta.get("trust", 1)
            applied = meta.get("applied", 0) + 1
            correct_cnt = meta.get("correct", 0) + (1 if correct else 0)
            # 신뢰도: 정답률 기반 (최소 0.1, 최대 2.0)
            new_trust = max(0.1, min(2.0, correct_cnt / applied * 2))
            col.update(ids=[doc_id],
                       metadatas=[{**meta,
                                   "trust": round(new_trust, 2),
                                   "applied": applied,
                                   "correct": correct_cnt}])
        logger.info("기법 신뢰도 업데이트: %s → correct=%s", category, correct)
    except Exception as e:
        logger.error("신뢰도 업데이트 실패: %s", e)


if __name__ == "__main__":
    learn()
