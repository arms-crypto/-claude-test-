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
import config
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


@app.route('/touch_timer', methods=['POST'])
def touch_timer():
    """PC LLM 타이머 갱신 — 서버보수 에이전트가 호출"""
    from llm_client import touch_ollama_request
    touch_ollama_request()
    return jsonify({"status": "ok", "message": "timer touched"}), 200


# ======================== 에러 모니터 대시보드 ========================

_dashboard_reset_time = None  # 리셋 기준 시각 (None=오늘 00:00)


def _get_dashboard_lines():
    """오늘 날짜 + 리셋 시각 이후 로그 라인만 반환."""
    from pathlib import Path
    from datetime import datetime
    import pytz
    log_path = Path('/home/ubuntu/-claude-test-/proxy_v54.log')
    if not log_path.exists():
        return []
    kst = pytz.timezone("Asia/Seoul")
    now = datetime.now(kst)
    today_str = now.strftime('%Y-%m-%d')
    reset_time = _dashboard_reset_time or now.replace(hour=0, minute=0, second=0, microsecond=0)
    result = []
    with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
        for line in f.readlines()[-10000:]:
            if today_str not in line[:11]:
                continue
            try:
                ts_str = line[:23]
                ts = kst.localize(datetime.strptime(ts_str, '%Y-%m-%d %H:%M:%S,%f'))
                if ts >= reset_time:
                    result.append(line)
            except Exception:
                result.append(line)
    return result


@app.route('/dashboard/reset', methods=['POST'])
def dashboard_reset():
    """대시보드 에러 카운트 리셋."""
    import pytz
    from datetime import datetime
    global _dashboard_reset_time
    _dashboard_reset_time = datetime.now(pytz.timezone("Asia/Seoul"))
    return jsonify({'status': 'ok', 'reset_time': _dashboard_reset_time.strftime('%H:%M:%S')})


@app.route('/dashboard', methods=['GET'])
def dashboard():
    """에러 모니터 대시보드 UI — 오늘 날짜 기준, 리셋 가능"""
    from collections import defaultdict
    from datetime import datetime
    import re
    import pytz

    error_patterns = {
        'KIS_FAILED':      r'KIS.*실패|KY.*실패',
        'TRACEBACK':       r'Traceback \(most recent call last\)',
        'ERROR':           r'\s+ERROR\s+',
        'HTTP_ERROR':      r'HTTPError:',
        'CONNECTION_ERROR':r'ConnectionError:',
        'TIMEOUT':         r'Timeout|timeout',
        'EXCEPTION':       r'Exception:',
        'JSON_ERROR':      r'JSONDecodeError|Expecting value',
        'DB_FAILED':       r'Oracle.*실패|ORA-',
        'PYKRX_ERROR':     r'pykrx',
    }

    lines = _get_dashboard_lines()
    errors = defaultdict(int)
    recent_errors = []

    for line in lines:
        for error_type, pattern in error_patterns.items():
            if re.search(pattern, line, re.IGNORECASE):
                errors[error_type] += 1
                if len(recent_errors) < 20:
                    recent_errors.append((error_type, line.strip()[:120]))
                break

    recent_errors = list(reversed(recent_errors))
    total_errors = sum(errors.values())
    status = 'HEALTHY' if total_errors < 20 else 'WARNING' if total_errors < 100 else 'CRITICAL'
    status_color = '#10b981' if status == 'HEALTHY' else '#f59e0b' if status == 'WARNING' else '#ef4444'
    max_cnt = max(errors.values()) if errors else 1
    now_str = datetime.now(pytz.timezone("Asia/Seoul")).strftime('%Y-%m-%d %H:%M:%S')
    reset_str = _dashboard_reset_time.strftime('%H:%M:%S') if _dashboard_reset_time else '00:00:00'
    line_count = len(lines)

    bars = ''.join(f'''
        <div class="bar">
            <div class="bar-label">{et}</div>
            <div class="bar-wrap"><div class="bar-fill" style="width:{cnt/max_cnt*100:.0f}%"></div></div>
            <div class="bar-val">{cnt}</div>
        </div>''' for et, cnt in sorted(errors.items(), key=lambda x: x[1], reverse=True))

    rows = ''.join(f'<tr><td class="et">{et}</td><td class="msg">{msg}</td></tr>'
                   for et, msg in recent_errors) or '<tr><td colspan="2" style="text-align:center;color:#64748b">에러 없음</td></tr>'

    html = f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>에러 대시보드</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:'Segoe UI',sans-serif;background:#0f172a;color:#e2e8f0;padding:20px}}
.wrap{{max-width:1100px;margin:0 auto}}
h1{{font-size:1.6em;margin-bottom:20px;color:#e2e8f0}}
.top{{display:flex;gap:12px;align-items:center;margin-bottom:20px;flex-wrap:wrap}}
.badge{{padding:6px 16px;border-radius:20px;font-weight:bold;font-size:0.9em;background:{status_color}22;color:{status_color};border:1px solid {status_color}}}
.info{{color:#94a3b8;font-size:0.85em}}
.btn{{padding:8px 18px;background:#ef444422;color:#ef4444;border:1px solid #ef4444;border-radius:6px;cursor:pointer;font-size:0.9em}}
.btn:hover{{background:#ef444433}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px}}
@media(max-width:700px){{.grid{{grid-template-columns:1fr}}}}
.card{{background:#1e293b;border:1px solid #334155;border-radius:8px;padding:16px}}
h2{{font-size:1em;color:#94a3b8;margin-bottom:12px;text-transform:uppercase;letter-spacing:.05em}}
.stat{{display:flex;justify-content:space-between;padding:10px;background:#0f172a;border-radius:6px;margin:6px 0}}
.sv{{font-size:1.8em;font-weight:bold;color:{status_color}}}
.bar{{display:flex;align-items:center;margin:7px 0}}
.bar-label{{width:130px;font-size:0.85em;color:#94a3b8}}
.bar-wrap{{flex:1;height:20px;background:#334155;border-radius:3px;overflow:hidden;margin:0 8px}}
.bar-fill{{height:100%;background:linear-gradient(90deg,#3b82f6,#8b5cf6)}}
.bar-val{{width:36px;text-align:right;font-size:0.9em;font-weight:bold}}
table{{width:100%;border-collapse:collapse;font-size:0.8em}}
td{{padding:6px 8px;border-bottom:1px solid #1e293b;vertical-align:top}}
.et{{width:120px;color:#f59e0b;font-weight:bold;white-space:nowrap}}
.msg{{color:#94a3b8;word-break:break-all}}
.footer{{text-align:center;color:#475569;font-size:0.8em;margin-top:16px}}
</style></head>
<body><div class="wrap">
<h1>🚨 에러 모니터 대시보드</h1>
<div class="top">
  <span class="badge">{status}</span>
  <span class="info">총 {total_errors}건 | {line_count}줄 분석 | 갱신 {now_str} | 리셋 기준 {reset_str}</span>
  <button class="btn" onclick="fetch('/dashboard/reset',{{method:'POST'}}).then(()=>location.reload())">🔄 리셋</button>
</div>
<div class="grid">
  <div class="card">
    <h2>📊 현황</h2>
    <div class="stat"><span>상태</span><span class="sv">{status}</span></div>
    <div class="stat"><span>총 에러</span><span style="font-size:1.8em;font-weight:bold">{total_errors}</span></div>
    <div class="stat"><span>분석 라인</span><span>{line_count:,}</span></div>
  </div>
  <div class="card">
    <h2>📈 에러 분포</h2>
    {bars or '<div style="color:#64748b;text-align:center;padding:20px">에러 없음</div>'}
  </div>
</div>
<div class="card">
  <h2>🔴 최근 에러 (최대 20건)</h2>
  <table><tbody>{rows}</tbody></table>
</div>
<div class="footer">자동 갱신 30초 | 리셋 버튼으로 카운트 초기화</div>
</div>
<script>setTimeout(()=>location.reload(),30000)</script>
</body></html>"""
    return html, 200, {'Content-Type': 'text/html; charset=utf-8'}


@app.route('/api/error-status', methods=['GET'])
def error_status_api():
    """에러 상태 JSON API"""
    from collections import defaultdict
    from datetime import datetime
    import re

    error_patterns = {
        'KIS_FAILED': r'KIS.*실패|KY.*실패',
        'TRACEBACK': r'Traceback \(most recent call last\)',
        'ERROR': r'\s+ERROR\s+',
        'HTTP_ERROR': r'HTTPError:',
        'CONNECTION_ERROR': r'ConnectionError:',
        'TIMEOUT': r'Timeout|timeout',
        'EXCEPTION': r'Exception:',
        'JSON_ERROR': r'JSONDecodeError|Expecting value',
        'DB_FAILED': r'Oracle.*실패|ORA-',
        'PYKRX_ERROR': r'pykrx',
    }

    lines = _get_dashboard_lines()
    errors = defaultdict(int)
    for line in lines:
        for error_type, pattern in error_patterns.items():
            if re.search(pattern, line, re.IGNORECASE):
                errors[error_type] += 1
                break

    total_errors = sum(errors.values())
    status = 'healthy' if total_errors < 20 else 'warning' if total_errors < 100 else 'critical'
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


@app.route('/auto_trade', methods=['POST'])
def auto_trade_toggle():
    """자동매매 시작/정지 API. {"action": "start"|"stop"}"""
    data = request.get_json(silent=True) or {}
    action = data.get("action", "")
    if action == "start":
        config._auto_enabled = True
    elif action == "stop":
        config._auto_enabled = False
    else:
        return jsonify({"error": "action must be start or stop"}), 400
    return jsonify({"status": "ok", "auto_enabled": config._auto_enabled})


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

    # 5) PC 슬립 워처 — 20분 유휴 시 자동 최대절전 (장중/장외 무관)
    def _sleep_watcher():
        import time as _t
        from llm_client import send_sleep
        while True:
            _t.sleep(60)
            send_sleep(delay_min=20)  # 장중/장외 무관 — 20분 유휴 시 최대절전

    threading.Thread(target=_sleep_watcher, daemon=True).start()

    # 6) Flask 웹 서버 실행
    app.run(host="0.0.0.0", port=11435)
