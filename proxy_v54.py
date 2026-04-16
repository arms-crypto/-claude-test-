#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
proxy_v54.py — Flask 라우트 + 메인 실행부 (모듈화 버전)
모든 비즈니스 로직은 각 모듈로 분리됨:
  config.py, db_utils.py, stock_data.py, search_utils.py,
  llm_client.py, ai_chat.py, telegram_bots.py, auto_trader.py
"""

import threading
import time
import logging

from flask import Flask, request, jsonify
from flask_cors import CORS

import matplotlib
matplotlib.rcParams['font.family'] = 'NanumGothic'
matplotlib.rcParams['axes.unicode_minus'] = False

# ⚠️ pykrx 로깅 버그 우회 (logging.info(args, kwargs) 형식 오류)
pykrx_logger = logging.getLogger('pykrx')
pykrx_logger.setLevel(logging.CRITICAL)

# 모듈 임포트
from config import logger
from db_utils import get_db_pool, ensure_db_initialized
from ai_chat import ask_ai
from llm_client import call_mistral_only
from search_utils import perplexica_search, search_and_summarize
from auto_trader import collect_smart_flows, get_smart_recommendations
from telegram_bots import handle_tg, handle_tg_srv, auto_report_scheduler
from auto_trader import auto_trade_loop, smart_wakeup_monitor
from mock_trading.telegram_handler import parse_mock_command

# -------------------------
# 앱 생성
app = Flask(__name__)
CORS(app)


# -------------------------
# Flask 엔드포인트

@app.route('/', methods=['GET'])
def index():
    """루트 엔드포인트 — 헬스체크용"""
    return jsonify({"service": "Ollama_Agent", "status": "running"}), 200


@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok"}), 200


# ======================== 에러 모니터 대시보드 ========================

@app.route('/dashboard', methods=['GET'])
def dashboard():
    """에러 모니터 대시보드 UI"""
    from pathlib import Path
    import json
    from datetime import datetime, timedelta
    from collections import defaultdict
    import re

    log_path = Path('/home/ubuntu/-claude-test-/proxy_v54.log')
    if not log_path.exists():
        return jsonify({"error": "log file not found"}), 404

    # 로그에서 에러 통계 추출
    errors = defaultdict(int)
    # 통합 에러 패턴 (CLAUDE.md 에러 모니터링 섹션 참조)
    error_patterns = {
        'ERROR': r'ERROR\s+',
        'TRACEBACK': r'Traceback \(most recent call last\)',
        'EXCEPTION': r'Exception:\s+',
        'TYPEERROR': r'TypeError:\s+',
        'ATTRIBUTEERROR': r'AttributeError:\s+',
        'JSON_ERROR': r'JSONDecodeError|Expecting value',
        'HTTP_ERROR': r'HTTPError:\s+',
        'CONNECTION_ERROR': r'ConnectionError:\s+',
        'TIMEOUT': r'Timeout|timeout',
        'LOB_BUG': r"'LOB' object is not subscriptable",
        'KIS_FAILED': r'KIS.*실패|KY.*실패',
        'DB_FAILED': r'Oracle.*실패|ORA-',
        'PYKRX_ERROR': r'pykrx',
    }

    with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
        lines = f.readlines()[-3000:]

    for line in lines:
        for error_type, pattern in error_patterns.items():
            if re.search(pattern, line, re.IGNORECASE):
                errors[error_type] += 1
                break

    total_errors = sum(errors.values())
    status = 'healthy' if total_errors < 50 else 'warning' if total_errors < 200 else 'critical'
    max_errors = max(errors.values()) if errors else 1

    # HTML 반환
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>🚨 에러 모니터 대시보드</title>
        <style>
            * {{ margin: 0; padding: 0; box-sizing: border-box; }}
            body {{
                font-family: 'Segoe UI', sans-serif;
                background: #0f172a;
                color: #e2e8f0;
                padding: 20px;
            }}
            .container {{ max-width: 1200px; margin: 0 auto; }}
            h1 {{
                margin-bottom: 30px;
                font-size: 2em;
                background: linear-gradient(135deg, #3b82f6, #8b5cf6);
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
            }}
            .grid {{
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
                gap: 20px;
                margin-bottom: 30px;
            }}
            .card {{
                background: #1e293b;
                border: 1px solid #334155;
                border-radius: 8px;
                padding: 20px;
                transition: all 0.3s;
            }}
            .card:hover {{ border-color: #64748b; box-shadow: 0 0 20px rgba(59, 130, 246, 0.2); }}
            .stat-box {{
                display: flex;
                justify-content: space-between;
                align-items: center;
                padding: 15px;
                background: #0f172a;
                border-radius: 6px;
                margin: 10px 0;
            }}
            .stat-label {{ font-size: 0.9em; color: #94a3b8; }}
            .stat-value {{ font-size: 2em; font-weight: bold; }}
            .status-healthy {{ color: #10b981; }}
            .status-warning {{ color: #f59e0b; }}
            .status-critical {{ color: #ef4444; }}
            .bar {{
                display: flex;
                align-items: center;
                margin: 10px 0;
            }}
            .bar-label {{ width: 120px; font-size: 0.9em; }}
            .bar-container {{
                flex: 1;
                height: 25px;
                background: #334155;
                border-radius: 4px;
                overflow: hidden;
                margin: 0 10px;
            }}
            .bar-fill {{
                height: 100%;
                background: linear-gradient(90deg, #3b82f6, #8b5cf6);
            }}
            .bar-value {{ width: 40px; text-align: right; font-weight: bold; }}
            .footer {{
                text-align: center;
                color: #64748b;
                margin-top: 30px;
                font-size: 0.9em;
            }}
            h2 {{ font-size: 1.2em; margin: 15px 0; color: #e2e8f0; }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>🚨 에러 모니터 대시보드</h1>

            <div class="grid">
                <div class="card">
                    <h2>📊 현재 상태</h2>
                    <div class="stat-box">
                        <div class="stat-label">상태</div>
                        <div class="stat-value status-{status}">{status.upper()}</div>
                    </div>
                    <div class="stat-box">
                        <div class="stat-label">총 에러</div>
                        <div class="stat-value">{total_errors}</div>
                    </div>
                    <div class="stat-box">
                        <div class="stat-label">마지막 갱신</div>
                        <div style="font-size: 0.9em;">{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</div>
                    </div>
                </div>

                <div class="card">
                    <h2>📈 에러 분포</h2>
                    {''.join(f'''
                    <div class="bar">
                        <div class="bar-label">{error_type}</div>
                        <div class="bar-container">
                            <div class="bar-fill" style="width: {(count / max_errors * 100):.0f}%"></div>
                        </div>
                        <div class="bar-value">{count}</div>
                    </div>
                    ''' for error_type, count in sorted(errors.items(), key=lambda x: x[1], reverse=True))}
                </div>

                <div class="card">
                    <h2>⚙️ 시스템</h2>
                    <div class="stat-box">
                        <div class="stat-label">모니터 포트</div>
                        <div style="font-family: monospace;">11435 (통합)</div>
                    </div>
                    <div class="stat-box">
                        <div class="stat-label">로그 파일</div>
                        <div style="font-family: monospace; font-size: 0.85em;">proxy_v54.log</div>
                    </div>
                    <div class="stat-box">
                        <div class="stat-label">상태 파일</div>
                        <div style="font-family: monospace; font-size: 0.85em;">status.json</div>
                    </div>
                    <div class="stat-box">
                        <div class="stat-label">보관 기간</div>
                        <div>7일 자동 삭제</div>
                    </div>
                </div>
            </div>

            <div class="footer">
                🚀 통합 에러 모니터 대시보드 v1.0 | 매 5초마다 자동 갱신
            </div>
        </div>

        <script>
            setTimeout(() => location.reload(), 5000);
        </script>
    </body>
    </html>
    """
    return html, 200, {'Content-Type': 'text/html; charset=utf-8'}


@app.route('/api/error-status', methods=['GET'])
def error_status_api():
    """에러 상태 JSON API"""
    from pathlib import Path
    from collections import defaultdict
    import re
    from datetime import datetime

    log_path = Path('/home/ubuntu/-claude-test-/proxy_v54.log')
    if not log_path.exists():
        return jsonify({"error": "log file not found"}), 404

    errors = defaultdict(int)
    # 통합 에러 패턴 (CLAUDE.md 에러 모니터링 섹션 참조)
    error_patterns = {
        'ERROR': r'ERROR\s+',
        'TRACEBACK': r'Traceback \(most recent call last\)',
        'EXCEPTION': r'Exception:\s+',
        'TYPEERROR': r'TypeError:\s+',
        'ATTRIBUTEERROR': r'AttributeError:\s+',
        'JSON_ERROR': r'JSONDecodeError|Expecting value',
        'HTTP_ERROR': r'HTTPError:\s+',
        'CONNECTION_ERROR': r'ConnectionError:\s+',
        'TIMEOUT': r'Timeout|timeout',
        'LOB_BUG': r"'LOB' object is not subscriptable",
        'KIS_FAILED': r'KIS.*실패|KY.*실패',
        'DB_FAILED': r'Oracle.*실패|ORA-',
        'PYKRX_ERROR': r'pykrx',
    }

    with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
        lines = f.readlines()[-3000:]

    for line in lines:
        for error_type, pattern in error_patterns.items():
            if re.search(pattern, line, re.IGNORECASE):
                errors[error_type] += 1
                break

    total_errors = sum(errors.values())
    status = 'healthy' if total_errors < 50 else 'warning' if total_errors < 200 else 'critical'

    return jsonify({
        'timestamp': datetime.now().isoformat(),
        'status': status,
        'total_errors': total_errors,
        'errors': dict(errors),
    })


@app.route('/ping_sleep_timer', methods=['GET'])
def ping_sleep_timer():
    """야간 배치 등 Ollama 미사용 작업 시작 전 슬립 타이머 리셋용 (로컬호스트 전용)"""
    if request.remote_addr not in ('127.0.0.1', '::1'):
        return jsonify({"error": "forbidden"}), 403
    from llm_client import touch_ollama_request
    touch_ollama_request()
    return jsonify({"status": "ok", "reset": True}), 200


@app.route('/ask', methods=['POST'])
def ask():
    msg = request.json.get("message", "")
    reply_text, _ = ask_ai("web_user", msg)
    return jsonify({"reply": reply_text})


@app.route('/mock', methods=['POST'])
def mock_trade():
    """
    모의투자 REST 엔드포인트.

    Request JSON:
        { "command": "/mock 현황" }
        { "command": "/mock 삼성전자 100만원 매수" }
        { "command": "/mock 매도 005930" }

    Response JSON:
        { "result": "..." }
    """
    data = request.json or {}
    command = data.get("command", "").strip()
    if not command:
        return jsonify({"error": "command 파라미터가 필요합니다."}), 400
    if not command.startswith("/mock"):
        command = "/mock " + command
    logger.info("/mock 요청: %s", command)
    result = parse_mock_command(command, oracle_pool=get_db_pool())
    return jsonify({"result": result})


@app.route('/search', methods=['POST'])
def search():
    """
    실시간 웹 검색 엔드포인트.

    Request JSON:
        {
            "query": "검색할 내용",
            "mode": "perplexica" | "searxng" | "auto"  (기본: "auto"),
            "focus": "webSearch" | "academicSearch" | ...  (Perplexica 전용, 기본: "webSearch")
        }

    Response JSON:
        {
            "query": "...",
            "answer": "...",
            "mode_used": "perplexica" | "searxng"
        }
    """
    data = request.json or {}
    query = data.get("query", "").strip()
    mode = data.get("mode", "auto")
    focus = data.get("focus", "webSearch")

    if not query:
        return jsonify({"error": "query 파라미터가 필요합니다."}), 400

    logger.info("/search 요청: query=%s mode=%s", query, mode)

    answer = None
    mode_used = mode

    if mode in ("perplexica", "auto"):
        answer = perplexica_search(query, focus_mode=focus)
        mode_used = "perplexica"

    if not answer or mode == "searxng":
        answer = search_and_summarize(query)
        mode_used = "searxng"

    return jsonify({"query": query, "answer": answer, "mode_used": mode_used})


@app.route('/agent', methods=['POST'])
def agent():
    """
    Claude Code → PC Ollama 에이전트 엔드포인트.

    Request JSON:
        { "prompt": "서버 디스크 사용량 확인해줘" }

    Response JSON:
        { "result": "..." }
    """
    data = request.json or {}
    prompt = data.get("prompt", "").strip()
    if not prompt:
        return jsonify({"error": "prompt 파라미터가 필요합니다."}), 400
    logger.info("/agent 요청: %s", prompt[:100])
    result = call_mistral_only(prompt, use_tools=True)
    logger.info("/agent 완료: %s", result[:100])
    return jsonify({"result": result})


@app.route('/save_news', methods=['POST'])
def save_news():
    """외부 뉴스 수집 스크립트(fetch_news.py 등)에서 Oracle daily_news에 저장.
    category 생략 시 키워드 기반 자동 분류."""
    data = request.json or {}
    text = data.get("text", "").strip()
    category = data.get("category")  # None이면 자동 분류
    if not text or len(text) < 15:
        return jsonify({"ok": False, "error": "text too short"}), 400
    from db_utils import save_fact_to_db
    save_fact_to_db(text[:1000], category=category)
    return jsonify({"ok": True})


@app.route('/collect_smart', methods=['GET', 'POST'])
def collect_smart():
    """cron(15:10 / 18:40) 호출용 — 기관/외국인 순매수 TOP100 수집"""
    date_str = request.args.get("date") or None
    ok, msg = collect_smart_flows(date_str)
    return jsonify({"ok": ok, "message": msg}), (200 if ok else 500)


@app.route('/smart', methods=['GET'])
def smart():
    """최근 7일 기관+외국인 중복 순매수 TOP10 추천"""
    rows, err = get_smart_recommendations()
    if err:
        return jsonify({"error": err}), 500
    if not rows:
        return jsonify({"message": "데이터 없음. /collect_smart 먼저 실행하세요.", "top10": []})
    result = []
    for i, row in enumerate(rows, 1):
        ticker, name, days_count, investor_count, total_net_buy = row
        result.append({
            "rank": i,
            "ticker": ticker,
            "name": name,
            "days_count": int(days_count),
            "investor_count": int(investor_count),
            "total_net_buy_억": int(total_net_buy) // 100_000_000
        })
    return jsonify({"top10": result, "description": "최근 7일 기관+외국인 중복 순매수 TOP10"})


# ─────────────────────────────────────────────────────────────────────────────
# 메인 실행부
if __name__ == "__main__":
    # 1) 앱 시작 시 DB 초기화를 백그라운드에서 한 번만 실행
    def _run_db_init_once():
        try:
            time.sleep(1)
            ensure_db_initialized()
        except Exception:
            logger.exception("백그라운드 DB 초기화 실패")

    def _init_tool_rag():
        """서버 시작 시 도구 정의를 RAG tool_memory에 저장 (1단계 RAG 초기화)."""
        try:
            time.sleep(3)
            from rag_store import store_tool_definitions
            from llm_client import _ALL_TOOLS
            n = store_tool_definitions(_ALL_TOOLS)
            logger.info("도구 RAG 초기화 완료: %d개 저장", n)
        except Exception:
            logger.exception("도구 RAG 초기화 실패")

    threading.Thread(target=_run_db_init_once, daemon=True).start()
    threading.Thread(target=_init_tool_rag, daemon=True).start()

    # 3) 업종 파라미터 로드
    try:
        import sector_params
        sector_params.load()
    except Exception:
        logger.exception("sector_params 로드 실패")

    # 2) 텔레그램 감시 스레드 실행
    threading.Thread(target=handle_tg, daemon=True).start()
    threading.Thread(target=handle_tg_srv, daemon=True).start()

    # 3) 자동 보고서 스케줄러 스레드 실행
    threading.Thread(target=auto_report_scheduler, daemon=True).start()

    # 3-1) 월간 리뷰 스케줄러 (매월 1일 09:00 KST)
    def _monthly_review_scheduler():
        """매월 1일 09:00 KST에 월간 리뷰 + 학습 실행."""
        import pytz
        import datetime as dt
        tz = pytz.timezone("Asia/Seoul")

        last_run_day = None
        while True:
            try:
                now = dt.datetime.now(tz)
                # 조건: day=1, hour=9, minute=0~1 (1분 윈도우)
                # last_run_day로 중복 실행 방지
                if (now.day == 1 and now.hour == 9 and
                    now.minute <= 1 and last_run_day != now.day):

                    logger.info("[월간리뷰] 매월 1일 09:00 — monthly_review() + monthly_learn() 시작")

                    try:
                        import sector_params
                        review_result = sector_params.monthly_review()
                        logger.info("[월간리뷰] ✅ monthly_review 완료: %s", review_result.get("status"))

                        learn_result = sector_params.monthly_learn()
                        logger.info("[월간리뷰] ✅ monthly_learn 완료: %s", learn_result.get("status"))

                        last_run_day = now.day  # 오늘은 더 이상 실행 안 함
                    except Exception as e:
                        logger.error("[월간리뷰] 실행 중 에러: %s", e, exc_info=True)
                else:
                    # 매월 1일이 아니면 last_run_day 리셋
                    if now.day != 1:
                        last_run_day = None

            except Exception as e:
                logger.error("[월간리뷰] 스케줄러 에러: %s", e)

            time.sleep(60)  # 1분마다 체크

    threading.Thread(target=_monthly_review_scheduler, daemon=True).start()

    # 4) 30초 포트폴리오 자동매매 스레드 실행
    threading.Thread(target=auto_trade_loop, daemon=True).start()

    # 4-1) 스마트 웨이크업 모니터 — 순매수 신규진입 + 차트신호 급변 시 PC 자동 웨이크업
    threading.Thread(target=smart_wakeup_monitor, daemon=True).start()

    # 5) PC 슬립 워처 — 10분 유휴 시 자동 최대절전 (장중/장외 무관)
    def _sleep_watcher():
        import time as _t
        from llm_client import send_sleep
        while True:
            _t.sleep(60)
            send_sleep(delay_min=10)  # 장중/장외 무관 — 10분 유휴 시 최대절전

    threading.Thread(target=_sleep_watcher, daemon=True).start()

    # 6) Flask 웹 서버 실행
    app.run(host="0.0.0.0", port=11435)
