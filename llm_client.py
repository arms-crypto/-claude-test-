#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
llm_client.py — LLM + WoL
send_wol(), wait_for_ollama(), _parse_ollama_response(),
Tool 상수들, _execute_tool_call(), call_mistral_only(),
call_qwen (= call_mistral_only), call_local_llm(), _ollama_alive()
"""

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

_ALL_TOOLS = [_WEB_SEARCH_TOOL, _STOCK_PRICE_TOOL, _NEWS_TOOL]

_TOOL_SYSTEM = """당신은 친근한 한국어 AI 어시스턴트입니다.
- 인사("안녕", "하이", "ㅎㅇ" 등)나 짧은 잡담에는 짧게 인사나 잡담으로만 답하세요.
- 최신 정보(뉴스, 현재 날씨, 현재 인물, 최신 사건 등)가 필요하면 반드시 web_search 도구를 호출하세요.
- 훈련 데이터 이후 사건(2024년 이후 포함)은 web_search로 확인하세요.
- 답변은 항상 한국어로 작성하세요.

[사용 가능한 로컬 데이터]
- DB 최신 뉴스: Oracle DB daily_news 테이블에 자동 수집된 뉴스 저장됨
- 시장 보고서: /home/ubuntu/.openclaw/workspace-research/data/market_report.txt (매일 20:00 KST 갱신)
- 포트폴리오: SQLite /home/ubuntu/-claude-test-/mock_trading/portfolio.db
- [참고 데이터] 섹션이 프롬프트에 포함되면 그 수치를 우선 사용하세요."""


def _execute_tool_call(tool_name: str, arguments: dict) -> str:
    """Ollama가 호출한 도구를 실행하고 결과를 반환."""
    # 지연 import (순환 참조 방지)
    from search_utils import searxng_search
    from stock_data import stock_price_overseas, korea_invest_stock, naver_news

    query = arguments.get("query", "")
    if tool_name == "web_search":
        logger.info("Ollama tool call: web_search('%s')", query)
        results = searxng_search(query, max_results=5)
        if results:
            lines = []
            for r in results[:5]:
                title = r.get("title", "")
                content = r.get("content", "")[:200]
                url = r.get("url", "")
                lines.append(f"- {title}: {content} ({url})")
            return "\n".join(lines)
        return "검색 결과 없음"
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
    return f"알 수 없는 도구: {tool_name}"


def call_mistral_only(prompt: str, system: str = _TOOL_SYSTEM, use_tools: bool = True) -> str:
    """
    mistral-small:24b 단독 호출. tool calling 지원.
    - use_tools=True: Ollama가 web_search 도구를 스스로 호출 가능
    - 3회 재시도 후 최종 실패 시 안내 메시지 반환.
    """
    config.WOL_SENT = False   # 매 요청마다 초기화 (이전 실패 후 재시도 시 WoL 재전송 허용)
    messages = [
        {"role": "system", "content": system},
        {"role": "user",   "content": prompt},
    ]
    payload = {
        "model": config.QWEN_MODEL,
        "messages": messages,
        "options": {"temperature": 0.7, "num_predict": 2048},
        "stream": False,
    }
    if use_tools:
        payload["tools"] = _ALL_TOOLS

    last_exc = None
    for attempt in range(1, config.MISTRAL_MAX_RETRY + 1):
        try:
            r = requests.post(config.QWEN_URL, json=payload, timeout=(1, 300))
            r.raise_for_status()
            config.WOL_SENT = False   # 연결 성공 → 플래그 초기화
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
                    })
                # 검색 결과를 받아 최종 답변 생성
                payload2 = {
                    "model": config.QWEN_MODEL,
                    "messages": messages,
                    "options": {"temperature": 0.7, "num_predict": 2048},
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
            # 연결 오류 = PC 절전 가능성 → WoL 시도 (1회만)
            if not config.WOL_SENT and any(k in err_str for k in ["connect", "refused", "timeout", "unreachable"]):
                logger.warning("Ollama 연결 실패 — PC 절전 의심, WoL 전송 시도")
                send_wol()
                # 텔레그램으로 알림 (백그라운드)
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
                    continue   # 바로 재시도
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


def call_local_llm(prompt: str, use_tools: bool = True) -> str:
    """로컬 Ollama 호출 — tool calling 지원 (llama3.2:3b)"""
    _no_proxy = {"http": None, "https": None}
    messages = [
        {"role": "system", "content": _TOOL_SYSTEM},
        {"role": "user", "content": prompt},
    ]
    payload = {
        "model": config.LOCAL_MODEL,
        "messages": messages,
        "stream": False,
    }
    if use_tools:
        payload["tools"] = _ALL_TOOLS
    try:
        r = requests.post(config.LOCAL_OLLAMA_URL, json=payload, timeout=180, proxies=_no_proxy)
        r.raise_for_status()
        msg = r.json().get("message", {})

        tool_calls = msg.get("tool_calls")
        if tool_calls:
            messages.append({"role": "assistant", "content": "", "tool_calls": tool_calls})
            for tc in tool_calls:
                fn = tc.get("function", {})
                tool_result = _execute_tool_call(fn.get("name", ""), fn.get("arguments", {}))
                messages.append({"role": "tool", "content": tool_result})
            payload2 = {**payload, "messages": messages}
            del payload2["tools"]  # 재호출시 tools 제거해 최종 답변 유도
            r2 = requests.post(config.LOCAL_OLLAMA_URL, json=payload2, timeout=180, proxies=_no_proxy)
            r2.raise_for_status()
            return r2.json().get("message", {}).get("content", "⚠️ 응답 파싱 실패")

        return msg.get("content", "⚠️ 응답 파싱 실패")
    except Exception as e:
        logger.exception("로컬 LLM 호출 실패")
        return f"⚠️ 오류: {e}"
