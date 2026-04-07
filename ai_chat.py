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
# 응답 품질 학습 — 좋은 응답은 RAG에, 나쁜 응답은 교정 후 RAG에
_BAD_PATTERNS = ["2023년", "2024년 이전", "학습 데이터", "학습된 데이터", "지식 한계",
                 "훈련 데이터", "학습 날짜", "제 지식은", "알 수 없습니다만"]
_GOOD_RESPONSE_COUNTER = [0]  # 좋은 응답 빈도 조절용

def learn_from_response(user_input: str, ai_response: str):
    """응답 품질을 판단해 RAG knowledge_memory에 저장."""
    try:
        from rag_store import store_knowledge
        has_bad = any(p in ai_response for p in _BAD_PATTERNS)

        if has_bad:
            # 나쁜 패턴 발견 → Qwen에게 교정 버전 생성 요청
            fix_prompt = (
                f"다음 AI 응답에서 '학습 데이터 날짜', '지식 한계' 같은 불필요한 면책 발언을 제거하고 "
                f"자연스럽게 다시 써줘. 핵심 내용은 유지.\n\n"
                f"원본: {ai_response[:400]}"
            )
            corrected = call_qwen(fix_prompt, use_tools=False)
            if corrected and len(corrected) > 20:
                example = f"Q: {user_input[:200]}\nA(교정): {corrected[:400]}"
                store_knowledge(example, category="corrected_response",
                                tags="면책발언교정,응답패턴")
                logger.info("나쁜 응답 교정 후 RAG 저장")
        else:
            # 좋은 응답 — 10회에 1번만 저장 (노이즈 방지)
            _GOOD_RESPONSE_COUNTER[0] += 1
            if _GOOD_RESPONSE_COUNTER[0] % 10 == 0 and len(ai_response) > 30:
                example = f"Q: {user_input[:200]}\nA: {ai_response[:400]}"
                store_knowledge(example, category="good_response",
                                tags="자연스러운응답,대화패턴")
                logger.info("좋은 응답 RAG 저장 (10회 주기)")
    except Exception:
        logger.exception("learn_from_response 예외")


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

    # 3-0) RAG 지식 베이스에서 유사 응답 패턴 검색 → 컨텍스트 주입
    try:
        from rag_store import search_knowledge
        _know = search_knowledge(user_input, n_results=2)
        if _know:
            fact_str = f"[응답 참고 예시]\n{_know}\n\n" + fact_str
    except Exception:
        pass
    # 히스토리를 messages 배열로 변환 (텍스트 혼합 방지)
    past_messages = list(history)[-8:] if len(history) > 0 else []
    chat_history_str = ""  # 더 이상 텍스트로 사용 안 함
    hist_msgs = []
    for i in range(0, len(past_messages) - 1, 2):
        if i + 1 < len(past_messages):
            u = past_messages[i].removeprefix("Human: ")
            a = past_messages[i + 1].removeprefix("AI: ")
            hist_msgs.append({"role": "user", "content": u})
            hist_msgs.append({"role": "assistant", "content": a})

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
    if any(k in _u for k in ["파일목록", "파일나열", ".py목록", ".py나열", "어떤파일", "뭐가있어", "뭐있어", "파일뭐", "ls"]):
        import subprocess as _sp
        _ls = _sp.run("ls /home/ubuntu/-claude-test-/*.py", shell=True, capture_output=True, text=True)
        if _ls.stdout.strip():
            _extra_ctx.append(f"[서버 .py 파일 목록]\n{_ls.stdout.strip()}")
    # 21시 이후 + 스캔 관련 키워드 → RAG에서 직접 주입 (도구 호출 없음, 20:35 스캔 완료 후)
    _after_market = now.hour >= 21
    _SCAN_RAG_KEYS = ["스캔결과", "스캔", "워치리스트", "매수신호", "신호종목", "내일참고",
                      "어제분석", "어젯밤", "야간분석", "분석결과"]
    if _after_market and any(k in _u for k in _SCAN_RAG_KEYS):
        try:
            from rag_store import search_scan
            _scan = search_scan("매수 신호 워치리스트", n_results=1)
            if _scan:
                _extra_ctx.append(f"[장 마감 후 워치리스트 스캔 결과]\n{_scan}")
        except Exception:
            pass
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

    # 3-2) 순매수 스캔 — Ollama 도구 호출 없이 직접 실행 후 반환
    _SCAN_SIGNAL_KEYS = ["매도신호", "매수신호", "관망종목", "스캔", "워치리스트스캔",
                         "신호종목", "매도종목", "매수종목", "살만한종목", "추천종목"]
    if "순매수" in _u and any(k in _u for k in _SCAN_SIGNAL_KEYS):
        from auto_trader import scan_buy_signals_for_chat
        # 기간 추출: N일 / N개월 / 오늘
        _days = None
        _months = 3
        _dm = re.search(r'(\d+)\s*일', user_input)
        _mm = re.search(r'(\d+)\s*개월', user_input)
        if "오늘" in user_input:
            _days = 1
        elif _dm:
            _days = int(_dm.group(1))
        elif _mm:
            _months = int(_mm.group(1))
        _scan_result = scan_buy_signals_for_chat(months=_months, days=_days)
        return _scan_result, None

    # 3-3) 차트 분석 — 신호 데이터를 미리 계산해 Ollama에 주입 (도구 호출 없음)
    _CHART_KEYS = ["차트분석", "차트봐", "매수인지", "매도인지", "관망인지",
                   "매수해도", "매도해도", "사도될까", "팔아도될까", "지금살까", "지금팔까"]
    _REPLAY_KEYS = ["다시보여", "다시봐", "방금분석", "아까분석", "이전분석", "다시줘"]
    # "다시 보여줄래" — 마지막 차트 분석 결과 캐시에서 반환
    if any(k in _u for k in _REPLAY_KEYS):
        _cached = config.store.get(f"__last_chart_{session_id}")
        if _cached:
            return _cached, None
    if any(k in _u for k in _CHART_KEYS):
        from auto_trader import analyze_chart_for_chat
        # 종목명/코드 추출 (6자리 숫자 우선, 없으면 앞 단어)
        _code_m = re.search(r'\b(\d{6})\b', user_input)
        _query = _code_m.group(1) if _code_m else user_input.strip()
        _chart_result = analyze_chart_for_chat(_query)
        # 세션별 마지막 차트 분석 결과 캐시
        config.store[f"__last_chart_{session_id}"] = _chart_result
        return _chart_result, None

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
        if extra_data:
            ctx += "[참고 데이터]\n" + "\n".join(extra_data) + "\n\n"

        prompt = f"{ctx}현재 시각: {current_time_str}\n질문: {user_input}"
        answer = call_qwen(prompt, history_messages=hist_msgs)

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
    threading.Thread(target=learn_from_response, args=(user_input, answer)).start()

    if any(k in answer for k in ["원", "$", "매수"]):
        threading.Thread(target=save_fact_to_db, args=(answer,)).start()

    return answer, image_path
