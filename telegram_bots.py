#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
telegram_bots.py — 텔레그램 봇
handle_mobile_command(), handle_tg(),
_srv_read_market_report(), _srv_query_portfolio(), _srv_get_server_status(),
gather_srv_context(), handle_tg_srv(), auto_report_scheduler()
"""

import os
import re
import sys
import time
import datetime
import threading
import subprocess
import requests
import pytz

import config
from db_utils import get_db_pool
from stock_data import (
    stock_price_overseas, korea_invest_stock, naver_news,
    get_foreign_net_buy, _get_today_institutional_net_buy
)
from search_utils import searxng_search, perplexica_search
from llm_client import call_mistral_only, call_gemma3, _ALL_TOOLS

logger = config.logger

MARKET_REPORT_PATH = "/home/ubuntu/.openclaw/workspace-research/data/market_report.txt"
PORTFOLIO_DB_PATH = "/home/ubuntu/-claude-test-/mock_trading/portfolio.db"

TRADING_SYSTEM_PROMPT = """당신은 주식 매매비서입니다. 다음 규칙을 엄격히 따르세요.

역할:
- 한국/미국 주식 매매 조언, 포트폴리오 현황 안내, 시장 동향 설명
- 서버 상태 확인 및 관리

## 절대 원칙: 환각 금지

**숫자/가격/통계는 반드시 [참고 데이터]에 있는 값만 사용한다.**
- [참고 데이터]에 없는 주가, 환율, 수익률은 절대 말하지 않는다
- "~일 것 같다", "~쯤 될 것 같다" 같은 추측 표현 금지
- 데이터가 없으면 "현재 데이터가 없어 확인이 필요합니다"라고 말한다

**포트폴리오/잔고는 [참고 데이터] 내 DB 조회 결과로만 답한다.**
기억 속 수치를 그대로 쓰지 않는다.

**모르면 솔직하게 말한다.**
그럴듯한 답 대신 "확인이 필요합니다"가 낫다.

## 답변 규칙
- 반드시 한국어로 답변
- 짧고 핵심만 — 불필요한 말 없이
- 인사말("안녕하세요!", "물론이죠!") 금지
- [참고 데이터] 수치 사용 시 출처 명시 (예: "시장보고서 기준", "DB 기준")
- 매매 조언 시 반드시 리스크 언급
- 근거 없는 예측/전망/의견 추가 금지 — 데이터 기반 사실만 전달
"""


def _srv_read_market_report() -> str:
    try:
        with open(MARKET_REPORT_PATH, "r", encoding="utf-8") as f:
            return f.read()[:2500]
    except Exception:
        return ""


def _srv_query_portfolio() -> str:
    import sqlite3 as _sq3
    try:
        lines = ["📊 포트폴리오 현황"]
        with _sq3.connect(PORTFOLIO_DB_PATH) as conn:
            cur = conn.cursor()
            try:
                cur.execute("SELECT value FROM account WHERE key='cash' LIMIT 1")
                acc = cur.fetchone()
                if acc:
                    lines.append(f"💰 잔고: {float(acc[0]):,.0f}원")
            except Exception:
                pass
            try:
                cur.execute("SELECT name, qty, avg_price FROM portfolio")
                rows = cur.fetchall()
                if rows:
                    lines.append("📈 보유종목:")
                    for r in rows:
                        lines.append(f"  • {r[0]} {r[1]}주 (평균 {float(r[2]):,.0f}원)")
                else:
                    lines.append("📈 보유종목 없음")
            except Exception:
                pass
            try:
                cur.execute("SELECT action, name, qty, price, created_at FROM trades ORDER BY created_at DESC LIMIT 5")
                trades = cur.fetchall()
                if trades:
                    lines.append("🔄 최근 거래:")
                    for t in trades:
                        lines.append(f"  • {str(t[4])[:10]} {t[0]} {t[1]} {t[2]}주 @{float(t[3]):,.0f}원")
            except Exception:
                pass
        return "\n".join(lines) if len(lines) > 1 else "포트폴리오 데이터 없음"
    except Exception as e:
        return f"DB 오류: {e}"


def _srv_get_server_status() -> str:
    try:
        r = requests.get("http://localhost:11435/health", timeout=5,
                         proxies={"http": None, "https": None})
        status = "✅ 정상" if r.status_code == 200 else f"❌ {r.status_code}"
        import datetime as _dt, pytz as _tz
        now = _dt.datetime.now(_tz.timezone("Asia/Seoul")).strftime("%H:%M:%S")
        thread_count = __import__("threading").active_count()
        return f"서버: {status} | {now} | 스레드 {thread_count}개"
    except Exception as e:
        return f"서버 응답 없음: {e}"


def gather_srv_context(text: str) -> str:
    """메시지 분석 후 관련 데이터를 수집해 컨텍스트 문자열 반환"""
    ctx = []
    t = re.sub(r'\s+', '', text).lower()

    if any(k in t for k in ["상태", "status", "서버", "프로세스", "작동", "실행중"]):
        ctx.append(f"[서버상태]\n{_srv_get_server_status()}")

    if any(k in t for k in ["포트폴리오", "잔고", "보유", "수익", "손익", "매매내역", "거래내역"]):
        ctx.append(f"[포트폴리오]\n{_srv_query_portfolio()}")

    if any(k in t for k in ["증시", "시장", "나스닥", "s&p", "코스피", "코스닥", "미장", "미국", "전망", "환율", "vix", "보고서"]):
        report = _srv_read_market_report()
        if report:
            ctx.append(f"[시장보고서]\n{report}")

    overseas_names = ["애플", "테슬라", "엔비디아", "구글", "아마존", "마이크로소프트", "ms", "메타", "넷플릭스", "apple", "tsla", "nvda", "goog", "amzn", "meta"]
    if any(k in t for k in overseas_names):
        result = stock_price_overseas(text)
        if result:
            ctx.append(f"[해외주가]\n{result}")

    if any(k in t for k in ["주가", "현재가", "얼마", "시세"]):
        candidates = re.findall(r'[가-힣]{2,}', text)
        skip = {"주가", "현재가", "얼마", "시세", "주식", "조회", "알려줘", "보여줘", "요즘", "매수", "매도"}
        for name in candidates:
            if name not in skip:
                result = korea_invest_stock(name)
                if result:
                    ctx.append(f"[한국주가-{name}]\n{result}")
                break

    if any(k in t for k in ["뉴스", "최신", "소식", "이슈", "검색"]):
        news = naver_news(text)
        if news and "실패" not in news and "없습니다" not in news:
            ctx.append(f"[네이버뉴스]\n{news}")
        web = searxng_search(text, max_results=3)
        if web:
            snippets = "\n".join(f"- {r['title']}: {r['content'][:120]}" for r in web if r.get("title"))
            if snippets:
                ctx.append(f"[웹검색]\n{snippets}")

    return "\n\n".join(ctx)


def handle_mobile_command(cmd):
    """모바일 제어 명령어 처리 (/restart /update /status /logs /smart)"""
    cmd = cmd.strip()
    script_dir = os.path.dirname(os.path.abspath(__file__))
    log_path = os.path.join(script_dir, "proxy_v54.log")

    if cmd == "/status":
        try:
            r = requests.get("http://localhost:11435/health", timeout=3, proxies={"http": None, "https": None})
            health = "✅ OK" if r.status_code == 200 else f"❌ {r.status_code}"
        except Exception:
            health = "❌ 응답없음"
        db = "✅ 연결됨" if get_db_pool() else "❌ 연결안됨"
        kst = pytz.timezone('Asia/Seoul')
        now = datetime.datetime.now(kst).strftime("%Y-%m-%d %H:%M:%S")
        thread_count = len(threading.enumerate())
        return f"📊 [서버 상태] {now}\n\n🌐 Flask: {health}\n🗄️ DB: {db}\n🧵 스레드: {thread_count}개"

    elif cmd == "/logs":
        try:
            result = subprocess.run(["tail", "-20", log_path], capture_output=True, text=True, timeout=5)
            logs = result.stdout.strip()
            return f"📋 [최근 로그]\n\n{logs[-3500:]}" if logs else "📋 로그 없음"
        except Exception as e:
            return f"❌ 로그 읽기 실패: {e}"

    elif cmd.startswith("/smart"):
        sub = cmd.split()[1] if len(cmd.split()) >= 2 else ""
        try:
            if sub == "1":
                return _get_today_institutional_net_buy() or "⚠️ 기관 데이터 없음 (장중 수집 전)"
            elif sub == "2":
                foreign = get_foreign_net_buy("순매수 TOP20")
                if "[IMAGE_PATH:" in foreign:
                    foreign = foreign.split("[IMAGE_PATH:")[0].strip()
                return foreign
            else:
                return "/smart 1 — 기관 순매수 TOP20\n/smart 2 — 외국인 순매수 TOP20"
        except Exception as e:
            return f"❌ 순매수 조회 실패: {e}"

    elif cmd == "/update":
        try:
            result = subprocess.run(
                ["git", "-C", script_dir, "pull", "origin", "claude/search-proxy-v53"],
                capture_output=True, text=True, timeout=30
            )
            output = (result.stdout + result.stderr).strip()
            return f"🔄 [git pull 결과]\n\n{output[:1000]}"
        except Exception as e:
            return f"❌ git pull 실패: {e}"

    elif cmd == "/restart":
        def do_restart():
            time.sleep(1)
            subprocess.run(
                ["git", "-C", script_dir, "pull", "origin", "claude/search-proxy-v53"],
                capture_output=True, timeout=30
            )
            os.execv(sys.executable, [sys.executable] + sys.argv)
        threading.Thread(target=do_restart, daemon=False).start()
        return "🔄 git pull 후 재시작합니다..."

    return None


def handle_tg():
    last_id = 0
    base_url = f"https://api.telegram.org/bot{config.TOKEN_RAW}"
    logger.info("텔레그램 감시 엔진 시작")
    while True:
        try:
            _no_proxy = {"http": None, "https": None}
            res = requests.get(f"{base_url}/getUpdates", params={"offset": last_id+1, "timeout": 10}, timeout=15, proxies=_no_proxy).json()
            if res.get("ok") and res.get("result"):
                for up in res["result"]:
                    last_id = up["update_id"]
                    msg = up.get("message", {})
                    if str(msg.get("chat", {}).get("id", "")) == config.CHAT_ID and "text" in msg:
                        msg_text = msg["text"]
                        logger.info("텔레그램 메시지 수신: %s", msg_text[:50])
                        # /mock 자동매매 → 자동매매 엔진으로 라우팅
                        if (msg_text.strip().startswith("/mock 자동매매")
                                or msg_text.strip() == "/mock 자동"):
                            from auto_trader import _handle_auto_trade_cmd
                            reply_text = _handle_auto_trade_cmd(msg_text.strip())
                            requests.post(f"{base_url}/sendMessage",
                                          json={"chat_id": config.CHAT_ID, "text": reply_text},
                                          proxies=_no_proxy)
                            continue
                        # /mock 명령어는 모의투자 핸들러로 라우팅
                        if msg_text.strip().startswith("/mock"):
                            from mock_trading.telegram_handler import parse_mock_command
                            reply_text = parse_mock_command(msg_text, oracle_pool=get_db_pool())
                            requests.post(f"{base_url}/sendMessage", json={"chat_id": config.CHAT_ID, "text": reply_text}, proxies=_no_proxy)
                            continue
                        from ai_chat import ask_ai
                        reply_text, image_path = ask_ai(config.CHAT_ID, msg_text)
                        requests.post(f"{base_url}/sendMessage", json={"chat_id": config.CHAT_ID, "text": reply_text}, proxies=_no_proxy)
                        if image_path and os.path.exists(image_path):
                            with open(image_path, "rb") as photo:
                                requests.post(f"{base_url}/sendPhoto", data={"chat_id": config.CHAT_ID}, files={"photo": photo}, proxies=_no_proxy)
                            os.remove(image_path)
        except Exception:
            logger.exception("handle_tg 루프 예외")
            time.sleep(5)


def handle_tg_srv():
    """oracleN_Agent_bot 폴링 — 백그라운드에서 도구+LLM 처리"""
    last_id = 0
    base_url = f"https://api.telegram.org/bot{config.TOKEN_SRV}"
    _no_proxy = {"http": None, "https": None}
    logger.info("oracleN 서버봇 시작")

    def _direct_reply(text: str):
        """LLM 없이 Python에서 바로 포맷해서 반환 (구조화 데이터)"""
        t = re.sub(r'\s+', '', text).lower()
        t_raw = text.lower()
        portfolio_kw = ["포트폴리오", "잔고", "보유종목", "수익률", "손익", "매매내역", "거래내역",
                        "db", "데이터베이스", "최신데이터", "내역"]
        if any(k in t for k in portfolio_kw):
            return _srv_query_portfolio()
        if any(k in t for k in ["서버상태", "상태확인", "서버확인"]) or \
           any(k in t_raw for k in ["서버 상태", "상태 확인"]):
            return _srv_get_server_status()
        if any(k in t for k in ["보고서", "marketreport", "증시", "시장", "나스닥", "s&p", "코스피", "코스닥", "미장", "vix", "환율"]):
            r = _srv_read_market_report()
            return r if r else "시장 보고서 없음 (매일 20:00 KST 갱신)"
        overseas = {"애플": "AAPL", "테슬라": "TSLA", "엔비디아": "NVDA", "구글": "GOOGL",
                    "아마존": "AMZN", "마이크로소프트": "MSFT", "메타": "META", "넷플릭스": "NFLX"}
        for name, ticker in overseas.items():
            if name in t or ticker.lower() in t:
                return stock_price_overseas(name)
        if any(k in t for k in ["주가", "현재가", "시세"]):
            candidates = re.findall(r'[가-힣]{2,}', text)
            skip = {"주가", "현재가", "시세", "주식", "조회", "알려줘", "보여줘"}
            for name in candidates:
                if name not in skip:
                    return korea_invest_stock(name)
        if any(k in t for k in ["뉴스", "최신", "소식", "이슈", "요약"]):
            query = re.sub(r'뉴스|최신|소식|이슈|요약', '', text).strip() or "증시"
            headlines = naver_news(query)
            if not headlines or "실패" in headlines:
                return "뉴스 조회 실패"
            try:
                r = requests.post(config.LOCAL_OLLAMA_URL, json={
                    "model": config.LOCAL_MODEL,
                    "messages": [
                        {"role": "system", "content": "뉴스 제목을 한국어로만 요약. 영어 절대 사용 금지. 1~3줄 번호 목록. 인사말/설명 없이 요약만."},
                        {"role": "user", "content": headlines}
                    ],
                    "stream": False
                }, timeout=60, proxies={"http": None, "https": None})
                summary = r.json().get("message", {}).get("content", "").strip()
                return f"📰 {query} 뉴스\n{summary}" if summary else headlines
            except Exception:
                return f"📰 {query} 뉴스\n{headlines}"
        return None

    GUIDE_MSG = (
        "🖥️ 서버 관리자\n\n"
        "[서버 관리]\n"
        "• /status — 서버 상태\n"
        "• /logs — 최근 로그\n"
        "• /smart — 외국인 순매수\n"
        "• /update — git pull\n"
        "• /restart — 재시작\n\n"
        "[조회]\n"
        "• 포트폴리오 / 잔고 / 내역\n"
        "• 시장 보고서 / 증시\n"
        "• [종목명] 주가 / 뉴스 [키워드]"
    )

    def _process(chat_id: str, text: str):
        try:
            _t = text.strip()
            if _t.startswith("/mock 자동매매") or _t == "/mock 자동":
                from auto_trader import _handle_auto_trade_cmd
                reply = _handle_auto_trade_cmd(_t)
                requests.post(
                    f"{base_url}/sendMessage",
                    json={"chat_id": chat_id, "text": reply},
                    proxies=_no_proxy,
                )
                return
            srv_reply = handle_mobile_command(text.strip())
            if srv_reply:
                reply = srv_reply
                requests.post(
                    f"{base_url}/sendMessage",
                    json={"chat_id": chat_id, "text": reply},
                    proxies=_no_proxy,
                )
                return
            direct = _direct_reply(text)
            if direct is not None:
                reply = direct
                requests.post(
                    f"{base_url}/sendMessage",
                    json={"chat_id": chat_id, "text": reply},
                    proxies=_no_proxy,
                )
                return
            # Gemma3 호출 전 즉시 "생각 중" 메시지 발송
            thinking_res = requests.post(
                f"{base_url}/sendMessage",
                json={"chat_id": chat_id, "text": "⏳ 생각 중..."},
                proxies=_no_proxy,
            ).json()
            thinking_msg_id = thinking_res.get("result", {}).get("message_id")
            try:
                reply = call_gemma3(text, use_tools=True) or GUIDE_MSG
            except Exception:
                reply = GUIDE_MSG
            # "생각 중" 메시지를 실제 답변으로 교체
            if thinking_msg_id:
                requests.post(
                    f"{base_url}/editMessageText",
                    json={"chat_id": chat_id, "message_id": thinking_msg_id, "text": reply},
                    proxies=_no_proxy,
                )
            else:
                requests.post(
                    f"{base_url}/sendMessage",
                    json={"chat_id": chat_id, "text": reply},
                    proxies=_no_proxy,
                )
            return
        except Exception as e:
            logger.exception("_process 오류")
            reply = f"⚠️ 처리 오류: {e}"
        requests.post(
            f"{base_url}/sendMessage",
            json={"chat_id": chat_id, "text": reply},
            proxies=_no_proxy,
        )

    while True:
        try:
            res = requests.get(
                f"{base_url}/getUpdates",
                params={"offset": last_id + 1, "timeout": 10},
                timeout=15,
                proxies=_no_proxy,
            ).json()
            if res.get("ok") and res.get("result"):
                for up in res["result"]:
                    last_id = up["update_id"]
                    msg = up.get("message", {})
                    chat_id = str(msg.get("chat", {}).get("id", ""))
                    if chat_id != config.CHAT_ID or "text" not in msg:
                        continue
                    text = msg["text"]
                    logger.info("oracleN 수신: %s", text[:50])
                    threading.Thread(target=_process, args=(chat_id, text), daemon=True).start()
        except Exception:
            logger.exception("handle_tg_srv 루프 예외")
            time.sleep(5)


def auto_report_scheduler():
    kst = pytz.timezone('Asia/Seoul')
    base_url = f"https://api.telegram.org/bot{config.TOKEN_RAW}"
    last_run_time = None
    logger.info("자동 보고서 스케줄러 시작")
    while True:
        try:
            now = datetime.datetime.now(kst)
            if now.weekday() < 5:
                if now.hour == 8 and now.minute == 40 and last_run_time != "morning":
                    _np = {"http": None, "https": None}
                    news_data = []
                    px = perplexica_search("오늘 주요 경제 뉴스 증시 전망")
                    if px and "찾지 못" not in px:
                        news_data.append(px)
                    nv = naver_news("경제 증시 뉴스")
                    if nv:
                        news_data.append(nv)
                    if news_data:
                        news_text = "\n".join(news_data[:2])
                        summary = call_mistral_only(
                            f"다음 뉴스를 3줄로 요약해줘:\n\n{news_text}",
                            system="뉴스 요약 전문가. 핵심만 3줄 한국어로."
                        )
                    else:
                        summary = "현재 검색 서버(Perplexica/SearXNG)가 응답하지 않습니다.\n`docker compose up -d` 로 재시작해보세요."
                    requests.post(f"{base_url}/sendMessage", json={"chat_id": config.CHAT_ID, "text": f"🌅 [장 시작 전 AI 프리뷰]\n\n{summary}"}, proxies=_np)
                    last_run_time = "morning"
                elif now.hour == 18 and now.minute == 0 and last_run_time != "afternoon":
                    _np = {"http": None, "https": None}

                    def _send(text):
                        requests.post(f"{base_url}/sendMessage",
                                      json={"chat_id": config.CHAT_ID, "text": text},
                                      proxies=_np, timeout=10)

                    foreign = get_foreign_net_buy("순매수")
                    _send(foreign if foreign and "실패" not in foreign
                          else "⚠️ 외국인 순매수 데이터 조회 실패")

                    inst = _get_today_institutional_net_buy()
                    if not inst:
                        # Oracle DB 미수집 시 네이버 실시간으로 폴백
                        from stock_data import _naver_net_buy_list
                        import pandas as _pd
                        df_inst_fb = _naver_net_buy_list('1000', '01', 'buy')
                        if df_inst_fb is not None and not df_inst_fb.empty:
                            kst_d = datetime.datetime.now(kst).strftime('%Y%m%d')
                            lines = [f"🏦 [{kst_d}] 기관 순매수 상위 {len(df_inst_fb)}선 (네이버)\n"]
                            for i, row in df_inst_fb.iterrows():
                                amt = row.get('금액', 0)
                                amt_str = f"({int(amt):,}백만원)" if _pd.notna(amt) else ""
                                lines.append(f"{i+1}위. {row['종목명']} {amt_str}")
                            inst = "\n".join(lines)
                        else:
                            inst = "⚠️ 기관 순매수 데이터 없음 (DB 미수집)"
                    _send(inst)

                    # 당일 청산 손익 통계
                    try:
                        import sqlite3 as _sq3
                        today_str = now.strftime('%Y-%m-%d')
                        con = _sq3.connect(PORTFOLIO_DB_PATH)
                        sell_rows = con.execute(
                            "SELECT pnl FROM trades WHERE action='SELL' AND created_at >= ? AND pnl IS NOT NULL",
                            [today_str]
                        ).fetchall()
                        con.close()
                        if sell_rows:
                            pnls      = [r[0] for r in sell_rows]
                            wins      = sum(1 for p in pnls if p > 0)
                            avg_pnl   = sum(pnls) / len(pnls)
                            stat_line = (f"📊 오늘 청산 {len(pnls)}건 | "
                                         f"승률 {wins}/{len(pnls)} ({wins/len(pnls)*100:.0f}%) | "
                                         f"평균손익 {avg_pnl:+.1f}%")
                        else:
                            stat_line = "📊 오늘 청산: 없음"
                    except Exception:
                        stat_line = ""

                    trade_part = ""
                    if config._daily_trade_log:
                        trade_part = ("📋 오늘 자동매매 내역:\n"
                                      + "\n".join(config._daily_trade_log)
                                      + (f"\n\n{stat_line}" if stat_line else ""))
                    else:
                        trade_part = f"📋 오늘 자동매매: 없음\n{stat_line}" if stat_line else "📋 오늘 자동매매: 없음"
                    config._daily_trade_log.clear()

                    px = perplexica_search("오늘 증시 마감 시황 뉴스")
                    nv = naver_news("증시 마감 시황")
                    news_src = px if (px and "찾지 못" not in px) else (nv or "")
                    if news_src:
                        news_summary = call_mistral_only(
                            f"다음 증시 뉴스를 2줄로 요약:\n\n{news_src}",
                            system="증시 뉴스 요약 전문가. 핵심만 2줄 한국어로."
                        )
                        _send(f"{trade_part}\n\n📰 오늘 증시 뉴스:\n{news_summary}")
                    else:
                        _send(trade_part)

                    last_run_time = "afternoon"
            if now.hour == 0 and now.minute == 0:
                last_run_time = None
                config._daily_trade_log.clear()
                # 자정: 오늘 cash → prev_day_cash 저장 (다음날 보수적 운영 판단용)
                try:
                    import sqlite3 as _sq3
                    con = _sq3.connect(PORTFOLIO_DB_PATH)
                    row = con.execute("SELECT value FROM account WHERE key='cash'").fetchone()
                    if row:
                        con.execute("INSERT OR REPLACE INTO account(key,value) VALUES('prev_day_cash',?)", [row[0]])
                        con.commit()
                    con.close()
                except Exception:
                    logger.exception("prev_day_cash 저장 실패")
                # 자정: RAG 뉴스/매매 자동 동기화
                try:
                    from rag_store import sync_news_from_db, sync_trades_from_db
                    n = sync_news_from_db(limit=30)
                    t = sync_trades_from_db(limit=50)
                    logger.info("RAG 자정 동기화: 뉴스 %d건, 매매 %d건", n, t)
                except Exception:
                    logger.exception("RAG 동기화 실패")
        except Exception:
            logger.exception("auto_report_scheduler 예외")
        time.sleep(30)
