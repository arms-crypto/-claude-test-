#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
llm_client.py — LLM + WoL
send_wol(), wait_for_ollama(), _parse_ollama_response(),
Tool 상수들, _execute_tool_call(), call_mistral_only(),
call_qwen (= call_mistral_only), _ollama_alive()
"""

import os
import json
import time
import threading
import requests

import config

logger = config.logger


# -------------------------
# Wake on LAN
def send_wol():
    """Wake on LAN: UDP 매직패킷 전송."""
    try:
        mac = config.WOL_MAC.replace(":", "").replace("-", "")
        magic = bytes.fromhex("F" * 12 + mac * 16)
        with __import__("socket").socket(__import__("socket").AF_INET, __import__("socket").SOCK_DGRAM) as s:
            s.setsockopt(__import__("socket").SOL_SOCKET, __import__("socket").SO_BROADCAST, 1)
            for _ in range(5):
                s.sendto(magic, (config.WOL_IP, 9))
                s.sendto(magic, (config.WOL_IP, 7))
        logger.info("WoL UDP 즉시 전송 완료 → %s", config.WOL_IP)
    except Exception as e:
        logger.error("WoL UDP 실패: %s", e)
    config.WOL_SENT = True
    return True


def wait_for_ollama(timeout: int = 120, interval: int = 10) -> bool:
    """Ollama가 응답할 때까지 대기. timeout초 내에 응답하면 True."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = requests.get(f"http://{config.REMOTE_OLLAMA_IP}:11434/api/tags", timeout=5)
            if r.status_code == 200:
                logger.info("Ollama 응답 확인 — PC 켜짐")
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def _parse_ollama_response(r) -> str:
    """Ollama chat 응답에서 텍스트 추출 (JSON / NDJSON 모두 처리)."""
    raw_text = r.text.strip()
    try:
        data = r.json()
        if isinstance(data, dict):
            if "message" in data:
                return data["message"]["content"]
            if "response" in data:
                return data["response"]
        if isinstance(data, list):
            parts = []
            for item in data:
                if isinstance(item, dict):
                    if "message" in item:
                        parts.append(item["message"].get("content", ""))
                    elif "response" in item:
                        parts.append(item["response"])
            return "\n".join(parts).strip()
        return str(data)
    except (json.JSONDecodeError, ValueError):
        # NDJSON (여러 줄 JSON) 처리
        for line in reversed(raw_text.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    if "message" in obj:
                        return obj["message"].get("content", "")
                    if "response" in obj:
                        return obj["response"]
            except Exception:
                continue
        return raw_text


# -------------------------
# Tool 상수들
_WEB_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "실시간 웹 검색. 최신 뉴스, 현재 정보, 모르는 사실, 훈련 데이터 이후 사건에 사용.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "검색어 (한국어 또는 영어)"}
            },
            "required": ["query"]
        }
    }
}

_STOCK_PRICE_TOOL = {
    "type": "function",
    "function": {
        "name": "get_stock_price",
        "description": "주식 현재가 실시간 조회. 종목명(한국어/영어)이나 티커(NVDA, 005930 등)로 조회. 주가·시세·현재가 질문에 반드시 사용.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "종목명 또는 티커. 예: '엔비디아', 'NVDA', 'SK하이닉스', '000660'"}
            },
            "required": ["query"]
        }
    }
}

_NEWS_TOOL = {
    "type": "function",
    "function": {
        "name": "get_news",
        "description": "네이버 실시간 뉴스 조회. 특정 종목·기업·시장 뉴스가 필요할 때 사용.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "검색어. 예: '엔비디아', '코스피', '반도체'"}
            },
            "required": ["query"]
        }
    }
}

_PORTFOLIO_TOOL = {
    "type": "function",
    "function": {
        "name": "query_portfolio",
        "description": "모의투자 포트폴리오 조회. 보유종목, 잔고, 손익, 매매내역 등 포트폴리오 관련 질문에 반드시 사용.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "조회 유형. 예: '현황', '잔고', '거래내역', '손익'"}
            },
            "required": ["query"]
        }
    }
}

_RAG_TOOL = {
    "type": "function",
    "function": {
        "name": "query_trade_history",
        "description": "과거 매매 이력(RAG) 조회. 특정 종목의 과거 매매 결과, 손익률, 매수/매도 시점 등을 물어볼 때 사용.",
        "parameters": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "종목코드 또는 종목명. 예: '005930', '삼성전자'"},
                "limit":  {"type": "integer", "description": "조회할 최근 거래 수 (기본 10)"}
            },
            "required": ["ticker"]
        }
    }
}

_DEEP_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "deep_search",
        "description": "AI 심층 검색 (Perplexity 스타일). 복잡한 질문, 다각도 분석, 배경 설명이 필요할 때 사용. web_search보다 느리지만 훨씬 상세한 답변.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "검색 질문 (구체적일수록 좋음)"}
            },
            "required": ["query"]
        }
    }
}

_FETCH_URL_TOOL = {
    "type": "function",
    "function": {
        "name": "fetch_url",
        "description": "특정 URL의 웹페이지 내용을 가져와 요약. 기사 링크나 공식 문서 URL을 직접 읽을 때 사용.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "읽을 웹페이지 URL (https://...)"}
            },
            "required": ["url"]
        }
    }
}

_LOCAL_KNOWLEDGE_TOOL = {
    "type": "function",
    "function": {
        "name": "search_local_knowledge",
        "description": "로컬 지식베이스 검색. 시장 보고서, 저장된 뉴스 등 서버에 캐시된 데이터 조회. 시황/증시 전망/최근 뉴스 요약에 활용.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "검색 키워드. 예: '코스피 전망', '오늘 뉴스'"}
            },
            "required": ["query"]
        }
    }
}

_ALL_TOOLS = [
    _WEB_SEARCH_TOOL, _DEEP_SEARCH_TOOL, _FETCH_URL_TOOL,
    _STOCK_PRICE_TOOL, _NEWS_TOOL, _LOCAL_KNOWLEDGE_TOOL,
    _PORTFOLIO_TOOL, _RAG_TOOL,
]

_TOOL_SYSTEM = """나는 한국어 지식 그래프 기반 AI 어시스턴트입니다. 사용자와의 대화에서 도구가 필요하면 도구를 호출하여 실시간 데이터를 기반으로 대화를 만들어내며 이를 기반으로 지식 그래프를 학습합니다. 절대로 수치를 추측하거나 만들지 마세요.

다음 서버 환경에서 실행되고 있으며, 이 환경에 실제로 접근할 수 있습니다.

[당신이 접근 가능한 데이터베이스 및 저장소]
- Oracle DB: daily_news 테이블에 매일 자동 수집된 뉴스 헤드라인이 저장됨
- SQLite portfolio.db: 모의투자 매매 기록, 보유종목, 잔고, 손익
- 시장 보고서 파일: 매일 갱신되는 코스피/코스닥 시장 분석 텍스트
사용자가 "DB", "저장된 것", "서버에 있는", "로컬 데이터" 등을 언급하면 위 데이터베이스를 말하는 것임.
→ 이 경우 반드시 도구를 호출해서 실제 데이터를 조회할 것. 절대 훈련 데이터로 추측하거나 생성하지 말 것.

[도구 선택 기준] — 절대로 훈련 데이터로 추측하지 말 것. 반드시 도구를 호출할 것.
- 주가/시세/현재가 → get_stock_price (시장 개장 여부 무관하게 항상 호출)
- 시황/증시/나스닥/코스피/미국주식/한국주식 동향 → web_search 또는 search_local_knowledge
- DB/저장소 관련 질문 → search_local_knowledge (Oracle DB 뉴스 + 시장 보고서 키워드 검색)
- 잔고, 보유종목, 거래내역 → query_portfolio (portfolio.db 직접 조회)
- 특정 종목 과거 매매 이력 → query_trade_history
- 종목/기업 뉴스 → get_news
- 간단한 최신 정보, 뉴스 헤드라인 → web_search
- 복잡한 분석, 심층 조사 → deep_search
- 특정 URL/기사 읽기 → fetch_url

[참고 데이터] 섹션이 프롬프트에 포함되면 그 수치를 우선 사용하세요."""


def _execute_tool_call(tool_name: str, arguments: dict) -> str:
    """Ollama가 호출한 도구를 실행하고 결과를 반환."""
    # 지연 import (순환 참조 방지)
    from search_utils import searxng_search
    from stock_data import stock_price_overseas, korea_invest_stock, naver_news

    query = arguments.get("query", "")
    if tool_name == "web_search":
        logger.info("Ollama tool call: web_search('%s')", query)
        results = searxng_search(query, categories="news", max_results=5)
        if not results:
            results = searxng_search(query, max_results=5)
        if results:
            lines = []
            for r in results[:5]:
                title = r.get("title", "")
                content = r.get("content", "")[:150]
                if content:
                    lines.append(f"- {title}: {content}")
            return "\n".join(lines) if lines else "검색 결과 없음"
        return "검색 결과 없음"
    if tool_name == "deep_search":
        logger.info("Ollama tool call: deep_search('%s')", query)
        from search_utils import perplexica_search
        result = perplexica_search(query)
        return result or "심층 검색 실패"
    if tool_name == "fetch_url":
        url = arguments.get("url", query)
        logger.info("Ollama tool call: fetch_url('%s')", url)
        try:
            import requests as _req
            from html.parser import HTMLParser
            resp = _req.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
            resp.encoding = resp.apparent_encoding or "utf-8"
            class _P(HTMLParser):
                def __init__(self):
                    super().__init__()
                    self.text = []
                    self._skip = False
                def handle_starttag(self, t, _a):
                    if t in ("script", "style", "nav", "header", "footer"): self._skip = True
                def handle_endtag(self, t):
                    if t in ("script", "style", "nav", "header", "footer"): self._skip = False
                def handle_data(self, d):
                    if not self._skip and d.strip(): self.text.append(d.strip())
            p = _P(); p.feed(resp.text)
            text = " ".join(p.text)[:3000]
            return f"[{url}]\n{text}" if text else "페이지 내용을 가져올 수 없습니다."
        except Exception as e:
            return f"URL 조회 실패: {e}"
    if tool_name == "get_stock_price":
        logger.info("Ollama tool call: get_stock_price('%s')", query)
        result = stock_price_overseas(query)
        if not result:
            result = korea_invest_stock(query)
        return result or f"'{query}' 주가 조회 실패"
    if tool_name == "get_news":
        logger.info("Ollama tool call: get_news('%s')", query)
        result = naver_news(query)
        return result or "뉴스 조회 실패"
    if tool_name == "search_local_knowledge":
        logger.info("Ollama tool call: search_local_knowledge('%s')", query)
        parts = []
        # RAG 벡터 검색 (의미 기반)
        try:
            from rag_store import search_memory
            rag_result = search_memory(query, n_results=5)
            if rag_result:
                parts.append(f"[RAG 기억 검색 결과]\n{rag_result}")
        except Exception:
            pass
        # 1) 시장 보고서 (키워드 포함 시 우선)
        rpt_path = "/home/ubuntu/.openclaw/workspace-research/data/market_report.txt"
        if os.path.exists(rpt_path):
            with open(rpt_path, "r", encoding="utf-8") as f:
                rpt = f.read()
            if not query or any(k in rpt for k in query.split()):
                parts.append(f"[시장 보고서]\n{rpt[:2000]}")
        # 2) Oracle DB 뉴스 — 키워드 검색
        try:
            from db_utils import get_db_pool
            pool = get_db_pool()
            if pool:
                with pool.acquire() as conn:
                    with conn.cursor() as cur:
                        # 키워드 포함된 최신 뉴스 우선, 없으면 최신 1건
                        cur.execute(
                            "SELECT headlines, run_time FROM daily_news "
                            "WHERE LOWER(headlines) LIKE LOWER(:kw) "
                            "ORDER BY run_time DESC FETCH FIRST 3 ROWS ONLY",
                            {"kw": f"%{query}%"}
                        )
                        rows = cur.fetchall()
                        if rows:
                            for r in rows:
                                parts.append(f"[DB 뉴스 {str(r[1])[:10]}]\n{r[0][:800]}")
                        else:
                            cur.execute("SELECT headlines, run_time FROM daily_news ORDER BY run_time DESC FETCH FIRST 1 ROWS ONLY")
                            row = cur.fetchone()
                            if row:
                                parts.append(f"[DB 최신 뉴스 {str(row[1])[:10]}]\n{row[0][:1500]}")
        except Exception:
            pass
        return "\n\n".join(parts) if parts else f"로컬 DB에 '{query}' 관련 저장된 데이터 없음"
    if tool_name == "query_portfolio":
        logger.info("Ollama tool call: query_portfolio('%s')", query)
        import sqlite3 as _sq3
        db_path = os.path.join(os.path.dirname(__file__), "mock_trading", "portfolio.db")
        try:
            lines = []
            with _sq3.connect(db_path) as con:
                row = con.execute("SELECT value FROM account WHERE key='cash'").fetchone()
                cash = int(float(row[0])) if row else 0
                lines.append(f"💰 현금잔고: {cash:,}원")
                holdings = con.execute(
                    "SELECT name, ticker, qty, avg_price FROM portfolio WHERE qty > 0"
                ).fetchall()
                if holdings:
                    lines.append("📈 보유종목:")
                    for name, ticker, qty, avg in holdings:
                        lines.append(f"  {name}({ticker}): {qty}주 @ 평단{int(avg):,}원")
                else:
                    lines.append("📭 보유종목 없음")
                # 컬럼 존재 여부에 따라 쿼리 분기
                cols = [r[1] for r in con.execute("PRAGMA table_info(trades)").fetchall()]
                pnl_col = ", pnl" if "pnl" in cols else ""
                recent = con.execute(
                    f"SELECT action, name, ticker, price, qty{pnl_col}, created_at "
                    "FROM trades ORDER BY id DESC LIMIT 5"
                ).fetchall()
                if recent:
                    lines.append("📋 최근 거래 (5건):")
                    for row in recent:
                        if pnl_col:
                            action, name, ticker, price, qty, pnl, ts = row
                        else:
                            action, name, ticker, price, qty, ts = row
                            pnl = None
                        pnl_str = f" | 손익 {pnl:+.1f}%" if pnl is not None else ""
                        lines.append(f"  [{ts[:10]}] {action} {name}({ticker}) {qty}주 @{int(price):,}원{pnl_str}")
            return "\n".join(lines)
        except Exception as e:
            return f"포트폴리오 조회 오류: {e}"
    if tool_name == "query_trade_history":
        ticker = arguments.get("ticker", query)
        limit  = int(arguments.get("limit", 10))
        logger.info("Ollama tool call: query_trade_history('%s', limit=%d)", ticker, limit)
        import sqlite3 as _sq3
        db_path = os.path.join(os.path.dirname(__file__), "mock_trading", "portfolio.db")
        try:
            with _sq3.connect(db_path) as con:
                cols = [r[1] for r in con.execute("PRAGMA table_info(trades)").fetchall()]
                extra = ", ".join(c for c in ["buy_signals", "rsi", "pnl"] if c in cols)
                sel = f"action, price, qty, created_at" + (f", {extra}" if extra else "")
                rows = con.execute(
                    f"SELECT {sel} FROM trades WHERE ticker=? OR name LIKE ? "
                    "ORDER BY id DESC LIMIT ?",
                    [ticker, f"%{ticker}%", limit]
                ).fetchall()
            if not rows:
                return f"'{ticker}' 관련 거래 내역 없음 (아직 거래 없음)"
            extra_cols = [c for c in ["buy_signals", "rsi", "pnl"] if c in cols]
            lines = [f"📚 {ticker} 과거 매매 이력 ({len(rows)}건):"]
            for row in rows:
                action, price, qty, ts = row[0], row[1], row[2], row[3]
                extras = {extra_cols[i]: row[4+i] for i in range(len(extra_cols))}
                pnl_str = f" | 손익 {extras['pnl']:+.1f}%" if extras.get("pnl") is not None else ""
                sig_str = f" | 신호 {extras['buy_signals']}/16" if extras.get("buy_signals") is not None else ""
                rsi_str = f" | RSI {extras['rsi']:.1f}" if extras.get("rsi") is not None else ""
                lines.append(f"  [{ts[:10]}] {action} {qty}주 @{int(price):,}원{pnl_str}{sig_str}{rsi_str}")
            return "\n".join(lines)
        except Exception as e:
            return f"거래 이력 조회 오류: {e}"
    return f"알 수 없는 도구: {tool_name}"


_GEMMA3_TOOL_SYSTEM = """나는 한국어 AI 어시스턴트입니다. 사용자와의 대화에서 도구가 필요하면 도구를 호출하여 실시간 데이터를 기반으로 대화를 만들어 냅니다. 절대로 수치를 추측하거나 만들지 마세요.

도구 호출 형식 — JSON 한 줄만, 다른 텍스트 없이:
{"tool":"도구명","arguments":{"query":"검색어"}}

[접근 가능한 데이터]
- Oracle DB: daily_news 테이블 (매일 수집된 뉴스 헤드라인) → search_local_knowledge
- 시장 보고서: 매일 갱신되는 코스피/코스닥 분석 텍스트 → search_local_knowledge
- SQLite portfolio.db: 모의투자 잔고·보유종목·거래내역 → query_portfolio
사용자가 "DB", "저장된", "서버", "로컬" 등을 언급하면 반드시 도구로 조회할 것.

사용 가능한 도구:
- get_stock_price: 주가·시세 조회 (종목명 또는 티커)
- get_news: 종목·기업·시장 뉴스
- web_search: 최신 정보·뉴스 검색
- search_local_knowledge: 시장보고서·DB뉴스·RAG 조회
- query_portfolio: 잔고·보유종목·거래내역
- query_trade_history: 특정 종목 과거 매매 이력
- deep_search: 복잡한 심층 분석
- fetch_url: 특정 URL 읽기

[예시]
사용자: 삼성전자 주가
{"tool":"get_stock_price","arguments":{"query":"삼성전자"}}

사용자: 애플 주가 조회해줘
{"tool":"get_stock_price","arguments":{"query":"AAPL"}}

사용자: 오늘 코스피 시황은?
{"tool":"get_news","arguments":{"query":"코스피 시황"}}

사용자: 내 포트폴리오 보여줘
{"tool":"query_portfolio","arguments":{"query":"현황"}}

사용자: 왕과 사는 남자 줄거리 요약
{"tool":"web_search","arguments":{"query":"왕과 사는 남자 영화 줄거리"}}

사용자: 안녕
안녕하세요! 무엇을 도와드릴까요?"""


def call_gemma3(prompt: str, use_tools: bool = True) -> str:
    """gemma3:4b 로컬 호출. 커스텀 tool calling (프롬프트 기반) 지원."""
    import datetime as _dt, pytz as _pytz, json as _json, re as _re
    _now = _dt.datetime.now(_pytz.timezone("Asia/Seoul"))
    # 날짜를 유저 메시지 앞에 붙임 → 시스템 프롬프트 고정 → Ollama KV 캐시 재사용
    _dated_prompt = f"[{_now.strftime('%Y-%m-%d %H:%M KST')}] {prompt}"
    messages = [
        {"role": "system", "content": _GEMMA3_TOOL_SYSTEM},
        {"role": "user",   "content": _dated_prompt},
    ]
    _tool_called = False  # 도구는 1회만 허용 (연쇄 호출 방지)
    for attempt in range(3):
        try:
            r = requests.post(
                config.LOCAL_OLLAMA_URL,
                json={
                    "model": "gemma3:4b",
                    "messages": messages,
                    "options": {"temperature": 0.7, "num_predict": 600, "num_ctx": 1024, "num_thread": 4},
                    "stream": False,
                },
                timeout=(5, 120),
                proxies={"http": None, "https": None},
            )
            r.raise_for_status()
            content = r.json().get("message", {}).get("content", "").strip()
            if not content:
                continue
            if not use_tools or _tool_called:
                return content
            # tool call 감지: 중첩 JSON 브레이스 카운팅으로 정확히 추출
            tool_data = None
            idx = content.find('{"tool"')
            if idx >= 0:
                depth, end = 0, idx
                for i, ch in enumerate(content[idx:]):
                    if ch == '{': depth += 1
                    elif ch == '}':
                        depth -= 1
                        if depth == 0:
                            end = idx + i + 1
                            break
                try:
                    tool_data = _json.loads(content[idx:end])
                except Exception:
                    pass
            if tool_data is None:
                try:
                    tool_data = _json.loads(content)
                except Exception:
                    pass
            if tool_data and "tool" in tool_data:
                tool_name = tool_data["tool"]
                args = tool_data.get("arguments", {})
                args = {k: (v["value"] if isinstance(v, dict) and "value" in v else v)
                        for k, v in args.items()}
                logger.info("Gemma3 tool call: %s(%s)", tool_name, args)
                tool_result = _execute_tool_call(tool_name, args)
                _tool_called = True
                # 요약 호출: 시스템 프롬프트를 단순화해서 도구 재호출 방지
                messages[0] = {"role": "system", "content": "한국어로 간결하게 답변하는 AI입니다. JSON이나 도구 호출 없이 텍스트로만 답하세요."}
                messages.append({"role": "assistant", "content": content})
                messages.append({"role": "user", "content": f"[검색 결과]\n{tool_result}\n\n위 내용을 바탕으로 한국어로 간결하게 답해줘."})
                continue
            return content
        except Exception as e:
            logger.error("Gemma3 호출 실패: %s", e)
            time.sleep(2)
    return "⚠️ 서버 AI 응답 실패"


def call_mistral_only(prompt: str, system: str = _TOOL_SYSTEM, use_tools: bool = True) -> str:
    """
    mistral-small:24b 단독 호출. tool calling 지원.
    - use_tools=True: Ollama가 web_search 도구를 스스로 호출 가능
    - 3회 재시도 후 최종 실패 시 안내 메시지 반환.
    """
    send_wol()                # 무조건 WoL 전송 (UDP, PC 켜져있어도 무해)
    _wol_waited = False       # 이번 호출에서 wait_for_ollama 대기 여부
    # 날짜를 유저 메시지 앞에 붙임 → 시스템 프롬프트 고정 → KV 캐시 재사용
    import datetime as _dt, pytz as _pytz
    _now = _dt.datetime.now(_pytz.timezone("Asia/Seoul"))
    _dated_prompt = f"[{_now.strftime('%Y-%m-%d %H:%M KST')}] {prompt}"
    messages = [
        {"role": "system", "content": system},
        {"role": "user",   "content": _dated_prompt},
    ]
    payload = {
        "model": config.QWEN_MODEL,
        "messages": messages,
        "options": {"temperature": 0.7, "num_predict": 1024, "num_ctx": 8192},
        "stream": False,
    }
    if use_tools:
        payload["tools"] = _ALL_TOOLS

    last_exc = None
    for attempt in range(1, config.MISTRAL_MAX_RETRY + 1):
        try:
            r = requests.post(config.QWEN_URL, json=payload, timeout=(1, 300))
            r.raise_for_status()
            data = r.json()
            msg = data.get("message", {})

            # tool_calls 처리: 도구 호출이 있으면 실행 후 재호출
            tool_calls = msg.get("tool_calls")
            if tool_calls:
                messages.append({"role": "assistant", "content": "", "tool_calls": tool_calls})
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    tool_result = _execute_tool_call(fn.get("name", ""), fn.get("arguments", {}))
                    messages.append({
                        "role": "tool",
                        "content": tool_result,
                        "tool_call_id": tc.get("id", ""),
                    })
                # 검색 결과를 받아 최종 답변 생성
                payload2 = {
                    "model": config.QWEN_MODEL,
                    "messages": messages,
                    "options": {"temperature": 0.7, "num_predict": 1024, "num_ctx": 8192},
                    "stream": False,
                }
                r2 = requests.post(config.QWEN_URL, json=payload2, timeout=(1, 300))
                r2.raise_for_status()
                result = _parse_ollama_response(r2)
                if result:
                    return result

            result = msg.get("content", "") or _parse_ollama_response(r)
            if result:
                return result
        except Exception as e:
            last_exc = e
            err_str = str(e).lower()
            # 연결 오류 = PC 절전 가능성 → 대기 (1회만)
            if not _wol_waited and any(k in err_str for k in ["connect", "refused", "timeout", "unreachable"]):
                _wol_waited = True
                logger.warning("Ollama 연결 실패 — PC 절전 의심, 응답 대기 중")
                def _notify():
                    try:
                        import telebot as _tb
                        _bot = _tb.TeleBot(config.TOKEN_RAW)
                        _bot.send_message(config.CHAT_ID, "💤 PC가 절전 상태입니다. Wake on LAN으로 깨우는 중...\n⏳ 1~2분 후 자동 재시도됩니다.")
                    except Exception:
                        pass
                threading.Thread(target=_notify, daemon=True).start()
                logger.info("Ollama 응답 대기 중 (최대 120초)...")
                if wait_for_ollama(timeout=180, interval=10):
                    continue
                else:
                    return "💤 PC가 절전 상태입니다. Wake on LAN으로 깨우는 중...\n⏳ 잠시 후 다시 말씀해 주세요. (보통 1~2분)"
            wait = 2 ** (attempt - 1)
            logger.warning("mistral-small:24b 시도 %d/%d 실패 (%s) — %ds 후 재시도",
                           attempt, config.MISTRAL_MAX_RETRY, str(e)[:80], wait)
            if attempt < config.MISTRAL_MAX_RETRY:
                time.sleep(wait)

    logger.error("mistral-small:24b %d회 모두 실패: %s", config.MISTRAL_MAX_RETRY, str(last_exc)[:200])
    return "⚠️ mistral 서버 불안정. 잠시 후 다시 시도해주세요.\n모의투자(/mock)는 정상 작동 중입니다."


# 기존 call_qwen 호출부 호환성 유지
call_qwen = call_mistral_only


def _ollama_alive() -> bool:
    """Ollama 응답 가능 여부를 1초 안에 확인."""
    try:
        r = requests.get(f"http://{config.REMOTE_OLLAMA_IP}:11434/api/tags", timeout=1)
        return r.status_code == 200
    except Exception:
        return False
