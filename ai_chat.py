#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ai_chat.py — AI 챗봇
get_session_history(), get_verified_facts(), get_fact_info_for_session(),
check_and_store_fact(), get_latest_db_news(), ask_ai()
"""

import re
import datetime
import threading
import logging
import pytz

import config
from db_utils import get_db_pool, get_stock_code_from_db, save_stock_code_to_db, save_fact_to_db
from stock_data import (
    get_yahoo_price, get_price_by_code, get_naver_price,
    stock_price_overseas, korea_invest_stock, get_foreign_net_buy
)
from search_utils import perplexica_search, search_and_summarize
from llm_client import call_mistral_only, call_qwen, send_wol, wait_for_ollama, _ollama_alive

logger = config.logger


# -------------------------
# Chat history helpers
def get_session_history(session_id):
    if session_id not in config.store:
        from collections import deque
        config.store[session_id] = deque(maxlen=20)
    return config.store[session_id]


def get_verified_facts(session_id):
    if session_id not in config.verified_facts_store:
        config.verified_facts_store[session_id] = []
    return "\n".join(config.verified_facts_store[session_id])


def get_fact_info_for_session(session_id: str) -> str:
    if session_id in config.verified_facts_store:
        return "\n".join(config.verified_facts_store[session_id])
    return ""


# -------------------------
# 팩트 검증 백그라운드
def check_and_store_fact(session_id, user_input, ai_response):
    check_prompt = f"""
    당신은 엄격한 팩트 체커입니다.
    다음 사용자의 말에서, 사용자의 '이름', '나이', '자산', '직업' 등 명확한 개인 정보나 영구적으로 기억해야 할 중요한 사실이 있다면 1문장으로 요약하세요.
    단, 농담, 인사말, 불확실한 미래 예측, 단순 질문이라면 무조건 'None'이라고만 대답하세요.

    사용자: {user_input}
    AI: {ai_response}
    """
    try:
        fact_result = call_qwen(check_prompt, use_tools=False)
        if "None" not in fact_result and len(fact_result.strip()) > 5:
            if session_id not in config.verified_facts_store:
                config.verified_facts_store[session_id] = []
            config.verified_facts_store[session_id].append(fact_result.strip())
            logger.info("팩트 검증 성공, 저장: %s", fact_result.strip())
    except Exception:
        logger.exception("check_and_store_fact 예외")


# -------------------------
# get_latest_db_news
def get_latest_db_news(query: str = "") -> str:
    p = get_db_pool()
    if not p:
        return "DB 연결 상태가 아닙니다."
    try:
        with p.acquire() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT headlines FROM daily_news ORDER BY run_time DESC FETCH FIRST 1 ROWS ONLY")
                res = cur.fetchone()
                return f"DB 최신 뉴스: {res[0]}" if res else "저장된 뉴스가 없습니다."
    except Exception:
        logger.exception("get_latest_db_news 예외")
        return "DB 조회 오류"


# -------------------------
# ask_ai 핵심 로직 (mistral-small:24b 기반)
def ask_ai(session_id, user_input):
    logger.info("ask_ai 진입: %s", user_input[:50])
    history = get_session_history(session_id)

    # 0) Ollama 상태 선확인 — 꺼져있으면 즉시 WoL 후 대기
    if not _ollama_alive():
        logger.warning("ask_ai: Ollama 꺼짐 감지 — 즉시 WoL 전송")
        send_wol()
        try:
            import telebot as _tb
            _tb.TeleBot(config.TOKEN_RAW).send_message(
                config.CHAT_ID, "💤 PC가 꺼져 있습니다. 깨우는 중...\n⏳ 1~2분 후 자동으로 답변드립니다."
            )
        except Exception:
            pass
        if not wait_for_ollama(timeout=180, interval=5):
            return "⏰ PC가 응답하지 않습니다. 잠시 후 다시 시도해주세요.", None
        logger.info("ask_ai: Ollama 복구 확인, 처리 계속")

    # 1) 6자리 코드만 입력시 자동학습
    if len(user_input.strip()) == 6 and user_input.strip().isdigit():
        code = user_input.strip()
        save_stock_code_to_db("코드학습", code)
        price = get_yahoo_price(code) or get_price_by_code(code) or get_naver_price(code)
        if price:
            return f"✅ `{code}` 자동학습! 💾\n현재가: **{price}**", None
        return f"❌ `{code}`: 가격 조회 실패", None

    # 2) 이름만 입력시 DB 조회 (3자 이상 + 숫자/영문 포함 또는 한자어만)
    clean_name = re.sub(r'\d{6}', '', user_input).strip()
    if clean_name and len(clean_name) >= 3 and not re.match(r'^[a-zA-Z가-힣]{1,2}$', clean_name):
        code = get_stock_code_from_db(clean_name)
        if code:
            price = get_yahoo_price(code) or get_price_by_code(code) or get_naver_price(code)
            if price:
                return f"**{clean_name}**: {price} (DB) 💾\n`[{code}]`", None

    # 3) 시간/팩트/채팅 기록 준비
    now = datetime.datetime.now(pytz.timezone('Asia/Seoul'))
    current_time_str = now.strftime("%Y년 %m월 %d일 %p %I:%M")
    my_facts = get_verified_facts(session_id)
    fact_str = f"[사용자 핵심 정보]\\n{my_facts}\\n\\n" if my_facts else ""
    past_messages = list(history)[-10:] if len(history) > 0 else []
    chat_history_str = "\\n".join(past_messages)

    # 3-1) DB/로컬 데이터 키워드 감지 → 컨텍스트 주입
    _u = re.sub(r'\s+', '', user_input).lower()
    _extra_ctx = []
    if any(k in _u for k in ["db뉴스", "db에", "최신뉴스", "저장된뉴스", "뉴스요약"]):
        _extra_ctx.append(get_latest_db_news())
    if any(k in _u for k in ["시장보고서", "market", "증시전망", "미장", "나스닥", "vix"]):
        # 지연 import (순환 참조 방지)
        from telegram_bots import _srv_read_market_report
        _rpt = _srv_read_market_report()
        if _rpt:
            _extra_ctx.append(f"[시장보고서]\n{_rpt[:2000]}")
    if _extra_ctx:
        fact_str = "[참고 데이터]\n" + "\n\n".join(_extra_ctx) + "\n\n" + fact_str

    # 3-0) 보고서 재요청 처리
    _req = user_input.strip()
    if any(k in _req for k in ["장 마감", "마감 보고서", "마감 AI"]) and any(k in _req for k in ["다시", "올려", "줘", "보여"]):
        from stock_data import naver_news
        net_buy = get_foreign_net_buy("순매수")
        img = None
        if "[IMAGE_PATH:" in (net_buy or ""):
            s = net_buy.find("[IMAGE_PATH:") + 12
            e = net_buy.find("]", s)
            img = net_buy[s:e]
            net_buy = net_buy[:s].strip()
        px = perplexica_search("오늘 증시 마감 시황 뉴스")
        nv = naver_news("증시 마감 시황")
        news_part = px if (px and "찾지 못" not in px) else (nv or "")
        parts = []
        if net_buy and "서버 점검" not in net_buy:
            parts.append(net_buy)
        if news_part:
            summary = call_mistral_only(
                f"다음 증시 뉴스를 2줄로 요약:\n\n{news_part}",
                system="증시 뉴스 요약 전문가. 핵심만 2줄 한국어로."
            )
            parts.append(f"📰 오늘 증시 뉴스:\n{summary}")
        reply = "📊 [장 마감 AI 요약 보고서]\n\n" + ("\n\n".join(parts) if parts else "현재 데이터를 가져올 수 없습니다.")
        return reply, img

    if any(k in _req for k in ["장 시작", "시작 전", "AI 프리뷰", "프리뷰"]) and any(k in _req for k in ["다시", "올려", "줘", "보여"]):
        from stock_data import naver_news
        px = perplexica_search("오늘 주요 경제 뉴스 증시 전망")
        nv = naver_news("경제 증시 뉴스")
        news = px if (px and "찾지 못" not in px) else (nv or "현재 뉴스를 가져올 수 없습니다.")
        news_text = news[:800]
        summary = call_mistral_only(f"다음 뉴스를 3줄로 요약:\n\n{news_text}", system="증시 뉴스 요약 전문가. 핵심만 3줄 한국어로.")
        return f"🌅 [장 시작 전 AI 프리뷰]\n\n{summary}", None

    # 4) 순매수/매매 데이터만 선수집 (Ollama가 처리하기 어려운 커스텀 API)
    extra_data = []
    if "순매수" in user_input.lower() or "순매매" in user_input.lower():
        fnb = get_foreign_net_buy(user_input)
        if fnb:
            extra_data.append("📈 외국인/기관 순매수:\n" + fnb)

    # 5) LLM 호출 — 모든 검색/뉴스/주가는 Ollama가 도구로 직접 판단
    try:
        ctx = ""
        if fact_str:
            ctx += fact_str
        if chat_history_str:
            ctx += f"이전 대화:\n{chat_history_str}\n\n"
        if extra_data:
            ctx += "[참고 데이터]\n" + "\n".join(extra_data) + "\n\n"

        prompt = f"{ctx}현재 시각: {current_time_str}\n질문: {user_input}"
        answer = call_qwen(prompt)

        # 6) LLM 응답에 6자리 종목코드 언급 + 주가 질문이면 자동 재조회
        if any(k in user_input for k in ["주가", "가격", "얼마", "시세"]):
            m6 = re.search(r'\b(\d{6})\b', answer)
            if m6:
                auto_price = korea_invest_stock(m6.group(1))
                if auto_price and not auto_price.startswith(("❌", "🚨")):
                    answer = auto_price + "\n\n" + answer

        image_path = None
        if "[IMAGE_PATH:" in answer:
            start = answer.find("[IMAGE_PATH:") + 12
            end = answer.find("]", start)
            if start >= 12 and end > start:
                image_path = answer[start:end]
                answer = answer[:start].strip()

    except Exception:
        logger.exception("ask_ai 내부 예외")
        answer = "시스템 오류가 발생했습니다. 잠시 후 다시 시도해주세요."

    # 7) 채팅 기록 및 팩트 저장
    history.append(f"Human: {user_input}")
    history.append(f"AI: {answer}")

    threading.Thread(target=check_and_store_fact, args=(session_id, user_input, answer)).start()

    if any(k in answer for k in ["원", "$", "매수"]):
        threading.Thread(target=save_fact_to_db, args=(answer,)).start()

    return answer, image_path
