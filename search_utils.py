#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
search_utils.py — 검색 유틸리티
searxng_search(), _get_perplexica_providers(), perplexica_search(),
search_and_summarize()
"""

import json
import requests

import config

logger = config.logger

# Perplexica 프로바이더 UUID 캐시 (컨테이너 재시작 시 갱신)
_perplexica_provider_cache = {"ollama_id": None, "trans_id": None}


def searxng_search(query: str, categories: str = "general", max_results: int = 5) -> list:
    """SearXNG에서 실시간 검색 결과를 가져온다."""
    try:
        r = requests.get(
            f"{config.SEARXNG_URL}/search",
            params={"q": query, "format": "json", "categories": categories, "language": "ko-KR"},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        results = data.get("results", [])[:max_results]
        return [
            {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "content": item.get("content", ""),
            }
            for item in results
        ]
    except Exception:
        logger.exception("SearXNG 검색 실패: %s", query)
        return []


def _get_perplexica_providers() -> tuple:
    """Perplexica /api/config에서 Ollama/Transformers 프로바이더 UUID를 가져온다."""
    global _perplexica_provider_cache
    if _perplexica_provider_cache["ollama_id"]:
        return _perplexica_provider_cache["ollama_id"], _perplexica_provider_cache["trans_id"]
    try:
        r = requests.get(f"{config.PERPLEXICA_URL}/api/config", timeout=10)
        providers = r.json().get("values", {}).get("modelProviders", [])
        ollama_id = trans_id = None
        for p in providers:
            if p.get("type") == "ollama":
                ollama_id = p["id"]
            elif p.get("type") == "transformers":
                trans_id = p["id"]
        _perplexica_provider_cache["ollama_id"] = ollama_id
        _perplexica_provider_cache["trans_id"] = trans_id
        return ollama_id, trans_id
    except Exception:
        return None, None


def perplexica_search(query: str, focus_mode: str = "webSearch") -> str:
    """Perplexica API를 통해 AI 검색 응답을 받는다.
    focus_mode: webSearch | academicSearch | writingAssistant | wolframAlphaSearch | youtubeSearch | redditSearch
    """
    import uuid as _uuid
    try:
        ollama_id, trans_id = _get_perplexica_providers()
        if not ollama_id:
            logger.warning("Perplexica 프로바이더 UUID 조회 실패")
            return None
        embed_id = trans_id or ollama_id
        embed_key = "Xenova/all-MiniLM-L6-v2" if trans_id else config.QWEN_MODEL
        payload = {
            "chatModel": {
                "providerId": ollama_id,
                "model": config.QWEN_MODEL,
                "key": config.QWEN_MODEL,
            },
            "embeddingModel": {
                "providerId": embed_id,
                "model": "all-MiniLM-L6-v2" if trans_id else config.QWEN_MODEL,
                "key": embed_key,
            },
            "optimizationMode": "speed",
            "focusMode": focus_mode,
            "message": {
                "content": query,
                "messageId": str(_uuid.uuid4()),
                "chatId": str(_uuid.uuid4()),
            },
            "history": [],
        }
        r = requests.post(
            f"{config.PERPLEXICA_URL}/api/chat",
            json=payload,
            timeout=30,
        )
        r.raise_for_status()
        r.encoding = 'utf-8'
        # 새 NDJSON 포맷 파싱: block/updateBlock/messageEnd
        text_blocks = {}
        sources = []
        for line in r.text.strip().splitlines():
            try:
                obj = json.loads(line)
                t = obj.get("type")
                if t == "block":
                    b = obj["block"]
                    if b["type"] == "text":
                        text_blocks[b["id"]] = b.get("data", "")
                    elif b["type"] == "source":
                        sources = b.get("data", [])
                elif t == "updateBlock":
                    bid = obj["blockId"]
                    for p in obj.get("patch", []):
                        if p.get("op") == "replace" and p.get("path") == "/data":
                            text_blocks[bid] = p["value"]
            except Exception:
                continue
        answer = list(text_blocks.values())[-1] if text_blocks else ""
        if sources:
            src_lines = "\n".join(
                f"- [{s.get('metadata', {}).get('title', s.get('pageContent','')[:40])}]({s.get('metadata', {}).get('url', '')})"
                for s in sources[:3]
            )
            answer += f"\n\n**출처:**\n{src_lines}"
        return answer if answer else "검색 결과를 찾지 못했습니다."
    except Exception:
        logger.exception("Perplexica 검색 실패: %s", query)
        # UUID 캐시 초기화 (다음 시도 시 재조회)
        _perplexica_provider_cache["ollama_id"] = None
        return None


def search_and_summarize(query: str) -> str:
    """SearXNG로 검색 후 Ollama(mistral-small:24b)로 요약 - Perplexica 장애 시 폴백."""
    # 지연 import (순환 참조 방지)
    from llm_client import call_mistral_only
    results = searxng_search(query, max_results=5)
    if not results:
        return "검색 결과가 없습니다."
    snippets = "\n\n".join(
        f"[{i+1}] {r['title']}\n{r['content']}\nURL: {r['url']}"
        for i, r in enumerate(results)
    )
    prompt = (
        f"다음은 '{query}'에 대한 실시간 웹 검색 결과입니다.\n\n"
        f"{snippets}\n\n"
        "위 검색 결과를 바탕으로 핵심 내용을 한국어로 간결하게 요약해 주세요. "
        "출처 URL도 함께 언급해 주세요."
    )
    return call_mistral_only(prompt)
