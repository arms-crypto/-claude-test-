#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rag_store.py — 진화형 RAG 저장소
nomic-embed-text(서버 Ollama) + Chroma 벡터DB
- Oracle DB 뉴스 자동 임베딩/누적
- 매매 결과 임베딩/누적
- 시장 보고서 임베딩/누적
- 질문에서 관련 기억 검색
"""

import os
import json
import hashlib
import logging
import requests
import chromadb

import config

logger = logging.getLogger("proxy_v53")

_CHROMA_DIR = os.path.join(os.path.dirname(__file__), "rag_data")
_EMBED_URL  = "http://localhost:11434/api/embeddings"
_EMBED_MODEL = "nomic-embed-text"

_client = None
_news_col = None
_trade_col = None


def _get_client():
    global _client, _news_col, _trade_col
    if _client is None:
        _client    = chromadb.PersistentClient(path=_CHROMA_DIR)
        _news_col  = _client.get_or_create_collection("news_memory")
        _trade_col = _client.get_or_create_collection("trade_memory")
    return _client, _news_col, _trade_col


def _embed(text: str) -> list:
    """nomic-embed-text로 텍스트 임베딩."""
    try:
        r = requests.post(
            _EMBED_URL,
            json={"model": _EMBED_MODEL, "prompt": text[:2000]},
            timeout=30,
            proxies={"http": None, "https": None},
        )
        r.raise_for_status()
        return r.json().get("embedding", [])
    except Exception as e:
        logger.error("임베딩 실패: %s", e)
        return []


def _doc_id(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


# -------------------------
# 뉴스 임베딩 저장
def store_news(headline: str, date_str: str = "") -> bool:
    """뉴스 헤드라인을 임베딩해서 Chroma에 저장."""
    try:
        _, news_col, _ = _get_client()
        doc_id = _doc_id(headline)
        # 중복 체크
        existing = news_col.get(ids=[doc_id])
        if existing["ids"]:
            return False  # 이미 저장됨
        emb = _embed(headline)
        if not emb:
            return False
        news_col.add(
            ids=[doc_id],
            embeddings=[emb],
            documents=[headline],
            metadatas=[{"date": date_str, "type": "news"}],
        )
        logger.info("뉴스 RAG 저장: %s...", headline[:40])
        return True
    except Exception as e:
        logger.error("뉴스 저장 실패: %s", e)
        return False


# -------------------------
# 매매 결과 임베딩 저장
def store_trade(ticker: str, name: str, action: str, price: float,
                signals: int = 0, rsi: float = 0.0, pnl: float = None,
                date_str: str = "") -> bool:
    """매매 기록을 임베딩해서 Chroma에 저장."""
    try:
        _, _, trade_col = _get_client()
        text = (f"{date_str} {action} {name}({ticker}) @{int(price):,}원 "
                f"신호{signals}/16 RSI{rsi:.1f}"
                + (f" 손익{pnl:+.1f}%" if pnl is not None else ""))
        doc_id = _doc_id(text)
        existing = trade_col.get(ids=[doc_id])
        if existing["ids"]:
            return False
        emb = _embed(text)
        if not emb:
            return False
        trade_col.add(
            ids=[doc_id],
            embeddings=[emb],
            documents=[text],
            metadatas={"ticker": ticker, "name": name, "action": action,
                       "pnl": pnl or 0.0, "signals": signals, "date": date_str},
        )
        logger.info("매매 RAG 저장: %s", text[:60])
        return True
    except Exception as e:
        logger.error("매매 저장 실패: %s", e)
        return False


# -------------------------
# 관련 기억 검색
def search_memory(query: str, n_results: int = 3, collection: str = "both") -> str:
    """질문과 유사한 기억을 Chroma에서 검색해서 반환. 순매수/스캔 관련 질문은 scan_memory 우선."""
    try:
        _, news_col, trade_col = _get_client()
        emb = _embed(query)
        if not emb:
            return ""
        results = []

        # 순매수/스캔/신호 관련 질문이면 scan_memory 먼저
        _scan_keywords = ["순매수", "스캔", "매도신호", "매수신호", "워치리스트", "신호종목"]
        if any(k in query for k in _scan_keywords):
            scan_result = search_scan(query, n_results=1)
            if scan_result:
                results.append(scan_result)

        if collection in ("both", "news"):
            r = news_col.query(query_embeddings=[emb], n_results=n_results)
            for doc, meta in zip(r["documents"][0], r["metadatas"][0]):
                results.append(f"[뉴스 {meta.get('date','')}] {doc}")
        if collection in ("both", "trade"):
            r = trade_col.query(query_embeddings=[emb], n_results=n_results)
            for doc, meta in zip(r["documents"][0], r["metadatas"][0]):
                results.append(f"[매매기록] {doc}")
        return "\n".join(results) if results else ""
    except Exception as e:
        logger.error("RAG 검색 실패: %s", e)
        return ""


# -------------------------
# Oracle DB 뉴스 일괄 임베딩 (초기 로딩 / 주기적 업데이트)
def sync_news_from_db(limit: int = 30) -> int:
    """Oracle DB daily_news에서 최신 뉴스를 가져와 RAG에 저장."""
    try:
        from db_utils import get_db_pool
        pool = get_db_pool()
        if not pool:
            return 0
        stored = 0
        with pool.acquire() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT headlines, run_time FROM daily_news "
                    "ORDER BY run_time DESC FETCH FIRST :n ROWS ONLY",
                    {"n": limit}
                )
                rows = cur.fetchall()
        for headlines, run_time in rows:
            date_str = str(run_time)[:10]
            # Oracle LOB 타입 처리
            if hasattr(headlines, "read"):
                headlines = headlines.read()
            # 헤드라인은 • 로 구분된 여러 줄
            for line in str(headlines).split("\n"):
                line = line.strip().lstrip("•").strip()
                if len(line) > 10:
                    if store_news(line, date_str):
                        stored += 1
        logger.info("뉴스 RAG 동기화 완료: %d건 신규 저장", stored)
        return stored
    except Exception as e:
        logger.error("뉴스 DB 동기화 실패: %s", e)
        return 0


# -------------------------
# portfolio.db 매매기록 일괄 임베딩
def sync_trades_from_db(limit: int = 50) -> int:
    """portfolio.db SELL 기록을 RAG에 저장 (손익 포함)."""
    try:
        import sqlite3 as _sq3
        db_path = os.path.join(os.path.dirname(__file__), "mock_trading", "portfolio.db")
        stored = 0
        with _sq3.connect(db_path) as con:
            cols = [r[1] for r in con.execute("PRAGMA table_info(trades)").fetchall()]
            extra = ", ".join(c for c in ["buy_signals", "rsi", "pnl"] if c in cols)
            sel = f"ticker, name, action, price, created_at" + (f", {extra}" if extra else "")
            rows = con.execute(
                f"SELECT {sel} FROM trades ORDER BY id DESC LIMIT ?", [limit]
            ).fetchall()
        for row in rows:
            ticker, name, action, price, ts = row[0], row[1], row[2], row[3], row[4]
            extras = {}
            for i, c in enumerate([c for c in ["buy_signals","rsi","pnl"] if c in cols]):
                extras[c] = row[5+i]
            if store_trade(ticker, name, action, price,
                           signals=int(extras.get("buy_signals") or 0),
                           rsi=float(extras.get("rsi") or 0),
                           pnl=extras.get("pnl"),
                           date_str=str(ts)[:10]):
                stored += 1
        logger.info("매매 RAG 동기화 완료: %d건 신규 저장", stored)
        return stored
    except Exception as e:
        logger.error("매매 DB 동기화 실패: %s", e)
        return 0


# -------------------------
# 스캔 결과 저장 (고정 ID로 항상 최신 유지)
def store_scan_result(scan_text: str, period_label: str = "3개월") -> bool:
    """
    scan_buy_signals_for_chat() 결과를 RAG에 저장.
    같은 기간 결과는 덮어씀 (고정 ID 사용).
    collect_smart_flows 실행 후 자동 호출 권장.
    """
    try:
        _get_client()
        scan_col = _client.get_or_create_collection("scan_memory")
        doc_id = f"scan_{period_label}"  # 고정 ID → 덮어쓰기
        # 기존 삭제
        try:
            scan_col.delete(ids=[doc_id])
        except Exception:
            pass
        emb = _embed(scan_text[:2000])
        if not emb:
            return False
        import datetime as _dt
        scan_col.add(
            ids=[doc_id],
            embeddings=[emb],
            documents=[scan_text],
            metadatas=[{"period": period_label, "updated": _dt.datetime.now().strftime("%Y-%m-%d %H:%M")}],
        )
        logger.info("스캔 결과 RAG 저장: [%s] %d자", period_label, len(scan_text))
        return True
    except Exception as e:
        logger.error("스캔 저장 실패: %s", e)
        return False


def search_scan(query: str, n_results: int = 2) -> str:
    """스캔 결과 컬렉션에서 검색."""
    try:
        _get_client()
        scan_col = _client.get_or_create_collection("scan_memory")
        if scan_col.count() == 0:
            return ""
        emb = _embed(query)
        if not emb:
            return ""
        r = scan_col.query(query_embeddings=[emb], n_results=min(n_results, scan_col.count()))
        results = []
        for doc, meta in zip(r["documents"][0], r["metadatas"][0]):
            results.append(f"[순매수 스캔결과 {meta.get('period','')} / 갱신:{meta.get('updated','')}]\n{doc}")
        return "\n\n".join(results) if results else ""
    except Exception as e:
        logger.error("스캔 검색 실패: %s", e)
        return ""


# -------------------------
# 범용 지식 저장 (도구 행동 교정용 예시, 룰, FAQ 등)
def store_knowledge(text: str, category: str = "general", tags: str = "") -> bool:
    """
    범용 텍스트를 임베딩해서 Chroma에 저장.
    category: 'tool_example' | 'rule' | 'faq' | 'general'
    tags: 쉼표구분 키워드 (예: '순매수,매도신호,scan_buy_signals')
    """
    try:
        _, news_col, _ = _get_client()
        # knowledge 컬렉션 별도 사용
        knowledge_col = _client.get_or_create_collection("knowledge_memory")
        doc_id = _doc_id(text)
        existing = knowledge_col.get(ids=[doc_id])
        if existing["ids"]:
            logger.info("이미 저장된 지식: %s...", text[:40])
            return False
        emb = _embed(text)
        if not emb:
            return False
        knowledge_col.add(
            ids=[doc_id],
            embeddings=[emb],
            documents=[text],
            metadatas=[{"category": category, "tags": tags}],
        )
        logger.info("지식 RAG 저장: [%s] %s...", category, text[:60])
        return True
    except Exception as e:
        logger.error("지식 저장 실패: %s", e)
        return False


def search_knowledge(query: str, n_results: int = 3) -> str:
    """지식 베이스에서 유사 항목 검색."""
    try:
        _get_client()
        knowledge_col = _client.get_or_create_collection("knowledge_memory")
        if knowledge_col.count() == 0:
            return ""
        emb = _embed(query)
        if not emb:
            return ""
        r = knowledge_col.query(query_embeddings=[emb], n_results=min(n_results, knowledge_col.count()))
        results = []
        for doc, meta in zip(r["documents"][0], r["metadatas"][0]):
            results.append(f"[{meta.get('category','')}] {doc}")
        return "\n".join(results) if results else ""
    except Exception as e:
        logger.error("지식 검색 실패: %s", e)
        return ""


# -------------------------
# 1단계 RAG — 도구 정의 저장/검색 (read-only, 서버 시작 시 1회 저장)

def store_tool_definitions(tools: list) -> int:
    """
    _ALL_TOOLS 도구 정의를 tool_memory 컬렉션에 저장.
    서버 시작 시 1회 호출. 같은 도구명이 있으면 덮어씀.
    """
    try:
        _get_client()
        tool_col = _client.get_or_create_collection("tool_memory")
        stored = 0
        for tool in tools:
            fn = tool.get("function", tool)
            name = fn.get("name", "")
            desc = fn.get("description", "")
            params = fn.get("parameters", {})
            if not name:
                continue
            # 파라미터 텍스트 직렬화
            param_lines = []
            props = params.get("properties", {})
            required_list = params.get("required", [])
            for pname, pinfo in props.items():
                req = "필수" if pname in required_list else "선택"
                pdesc = pinfo.get("description", "")
                ptype = pinfo.get("type", "string")
                param_lines.append(f"  - {pname} ({ptype}, {req}): {pdesc}")
            text = (f"도구명: {name}\n"
                    f"설명: {desc}\n"
                    f"파라미터:\n" + ("\n".join(param_lines) if param_lines else "  없음"))
            doc_id = f"tool_{name}"
            try:
                tool_col.delete(ids=[doc_id])
            except Exception:
                pass
            emb = _embed(text)
            if emb:
                tool_col.add(
                    ids=[doc_id],
                    embeddings=[emb],
                    documents=[text],
                    metadatas=[{"name": name, "description": desc[:200]}],
                )
                stored += 1
        logger.info("도구 RAG 저장 완료: %d개", stored)
        return stored
    except Exception as e:
        logger.error("도구 RAG 저장 실패: %s", e)
        return 0


def search_tools(query: str, n_results: int = 5) -> str:
    """
    질문에 관련된 도구 정의를 tool_memory에서 검색해서 텍스트로 반환.
    tool_memory가 비어있으면 자동으로 _ALL_TOOLS 로딩 시도.
    """
    try:
        _get_client()
        tool_col = _client.get_or_create_collection("tool_memory")
        if tool_col.count() == 0:
            # 지연 초기화
            try:
                from llm_client import _ALL_TOOLS
                store_tool_definitions(_ALL_TOOLS)
            except Exception as _ie:
                logger.warning("도구 RAG 지연 초기화 실패: %s", _ie)
                return ""
        emb = _embed(query)
        if not emb:
            return ""
        n = min(n_results, tool_col.count())
        r = tool_col.query(query_embeddings=[emb], n_results=n)
        return "\n\n".join(r["documents"][0]) if r["documents"][0] else ""
    except Exception as e:
        logger.error("도구 RAG 검색 실패: %s", e)
        return ""


# -------------------------
# Ollama 자율 학습 기법 검색 (learn_chart_method.py로 구축)
def search_chart_method(query: str, n_results: int = 3, sector: str = None) -> str:
    """
    Ollama가 스스로 발견한 차트 분석 기법을 검색.
    sector 지정 시 해당 업종 문서 우선 반환 (전체 문서도 포함).
    매매/분석 시 참고용.
    """
    try:
        _get_client()
        col = _client.get_or_create_collection("chart_method_memory")
        if col.count() == 0:
            return ""
        emb = _embed(query[:1000])
        if not emb:
            return ""

        results = []

        # 업종 지정 시: 해당 업종 문서 먼저 메타데이터 필터로 조회
        if sector and sector != "전체":
            try:
                n_sec = min(2, col.count())
                r_sec = col.query(
                    query_embeddings=[emb],
                    n_results=n_sec,
                    where={"sector": sector}
                )
                for doc, meta in zip(r_sec["documents"][0], r_sec["metadatas"][0]):
                    cat = meta.get("category", "")
                    sec = meta.get("sector", "전체")
                    results.append(f"[학습기법/{cat}/{sec}]\n{doc}")
            except Exception:
                pass

        # 전체 임베딩 검색 (업종 문서 이미 있으면 n_results 줄임)
        n_remain = max(1, n_results - len(results))
        n = min(n_remain, col.count())
        r = col.query(query_embeddings=[emb], n_results=n)
        seen = {d[:50] for d in [x[0] for x in [results]]} if results else set()
        for doc, meta in zip(r["documents"][0], r["metadatas"][0]):
            if doc[:50] not in seen:
                cat = meta.get("category", "")
                sec = meta.get("sector", "전체")
                results.append(f"[학습기법/{cat}/{sec}]\n{doc}")

        return "\n\n".join(results) if results else ""
    except Exception as e:
        logger.error("기법 검색 실패: %s", e)
        return ""


# -------------------------
# 차트 패턴 검색 (build_signal_history.py로 구축된 학습 데이터)
def search_chart_pattern(query: str, n_results: int = 5) -> str:
    """
    현재 신호 상황과 유사한 과거 패턴을 chart_pattern_memory에서 검색.
    스캔/분석 시 Ollama가 참고할 과거 사례 반환.
    """
    try:
        _get_client()
        pat_col = _client.get_or_create_collection("chart_pattern_memory")
        stat_col = _client.get_or_create_collection("chart_pattern_stats")

        if pat_col.count() == 0 and stat_col.count() == 0:
            return ""

        emb = _embed(query[:2000])
        if not emb:
            return ""

        results = []

        # 1. 통계 요약 먼저 (패턴 조합별 승률)
        if stat_col.count() > 0:
            n = min(3, stat_col.count())
            r = stat_col.query(query_embeddings=[emb], n_results=n)
            for doc in r["documents"][0]:
                results.append(f"[과거패턴통계]\n{doc}")

        # 2. 개별 사례 (유사 상황)
        if pat_col.count() > 0:
            n = min(n_results, pat_col.count())
            r = pat_col.query(query_embeddings=[emb], n_results=n)
            for doc, meta in zip(r["documents"][0], r["metadatas"][0]):
                results.append(f"[유사사례 {meta.get('date','')} {meta.get('name','')}]\n{doc}")

        return "\n\n".join(results) if results else ""
    except Exception as e:
        logger.error("차트패턴 검색 실패: %s", e)
        return ""


# -------------------------
# 상태 확인
def rag_status() -> str:
    try:
        _get_client()
        news_col  = _client.get_or_create_collection("news_memory")
        trade_col = _client.get_or_create_collection("trade_memory")
        know_col  = _client.get_or_create_collection("knowledge_memory")
        tool_col  = _client.get_or_create_collection("tool_memory")
        pat_col   = _client.get_or_create_collection("chart_pattern_memory")
        stat_col  = _client.get_or_create_collection("chart_pattern_stats")
        return (f"📚 RAG 저장소\n"
                f"  뉴스 기억: {news_col.count()}건\n"
                f"  매매 기억: {trade_col.count()}건\n"
                f"  지식 베이스: {know_col.count()}건\n"
                f"  도구 정의: {tool_col.count()}개\n"
                f"  차트패턴 학습: {pat_col.count()}건\n"
                f"  패턴통계: {stat_col.count()}건")
    except Exception as e:
        return f"RAG 상태 확인 실패: {e}"


if __name__ == "__main__":
    print("RAG 초기 동기화 시작...")
    n = sync_news_from_db(limit=50)
    t = sync_trades_from_db(limit=100)
    print(f"뉴스 {n}건, 매매 {t}건 저장 완료")
    print(rag_status())
    print("\n검색 테스트: '반도체'")
    print(search_memory("반도체"))
