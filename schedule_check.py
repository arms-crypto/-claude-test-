#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
매일 오전 9시 모의투자 시스템 자동 점검 -> 텔레그램 결과 전송
실행: nohup python3 schedule_check.py &
"""

import schedule
import time
import requests
import subprocess
import logging
from datetime import datetime

# 텔레그램 설정
TOKEN   = "8707168013:AAH5yIsoaLoxcA0Lthiw7RaIzD1YcJx8cc8"
CHAT_ID = "8448138406"
BASE_URL = f"https://api.telegram.org/bot{TOKEN}"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger(__name__)


def send_telegram(text: str):
    try:
        requests.post(f"{BASE_URL}/sendMessage", json={
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "Markdown"
        }, timeout=10)
    except Exception as e:
        logger.error(f"텔레그램 전송 실패: {e}")


def check_endpoint(url: str, payload: dict, label: str) -> tuple[bool, str]:
    try:
        r = requests.post(url, json=payload, timeout=30)
        if r.status_code == 200:
            return True, f"✅ {label} 정상"
        return False, f"❌ {label} HTTP {r.status_code}"
    except requests.exceptions.ConnectionError:
        return False, f"❌ {label} 연결 실패 (서버 다운?)"
    except requests.exceptions.Timeout:
        return False, f"⚠️ {label} 타임아웃"
    except Exception as e:
        return False, f"❌ {label} 오류: {e}"


def check_docker() -> tuple[bool, str]:
    try:
        r = subprocess.run(
            ["docker", "compose", "ps", "--format", "json"],
            capture_output=True, text=True, cwd="/home/user/-claude-test-", timeout=15
        )
        if r.returncode != 0:
            return False, f"❌ Docker 상태 확인 실패: {r.stderr.strip()}"
        return True, "✅ Docker 컨테이너 정상"
    except FileNotFoundError:
        return False, "⚠️ Docker 명령어 없음"
    except Exception as e:
        return False, f"❌ Docker 오류: {e}"


def run_checks():
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    logger.info("점검 시작")

    results = []
    errors = []

    # 1. Docker
    ok, msg = check_docker()
    results.append(msg)
    if not ok:
        errors.append(msg)

    # 2. 헬스체크
    ok, msg = check_endpoint("http://localhost:11435/health", {}, "헬스체크")
    results.append(msg)
    if not ok:
        errors.append(msg)

    # 3. /ask 엔드포인트
    ok, msg = check_endpoint(
        "http://localhost:11435/ask",
        {"message": "삼성전자 오늘 주가"},
        "/ask 엔드포인트"
    )
    results.append(msg)
    if not ok:
        errors.append(msg)

    # 4. /search 엔드포인트
    ok, msg = check_endpoint(
        "http://localhost:11435/search",
        {"query": "코스피 시황", "mode": "searxng"},
        "/search 엔드포인트"
    )
    results.append(msg)
    if not ok:
        errors.append(msg)

    # 5. 모의투자 모듈
    try:
        import sys
        sys.path.insert(0, "/home/user/-claude-test-")
        from mock_trading.mock_trading import MockTrading  # noqa
        results.append("✅ 모의투자 모듈 정상")
    except Exception as e:
        msg = f"❌ 모의투자 모듈 오류: {e}"
        results.append(msg)
        errors.append(msg)

    # 결과 메시지 작성
    status = "🔴 이상 감지" if errors else "🟢 전체 정상"
    lines = [f"*[{now}] 모의투자 점검 결과*", f"*상태: {status}*", ""]
    lines += results

    if errors:
        lines += ["", "⚠️ *오류 목록:*"] + errors

    send_telegram("\n".join(lines))
    logger.info(f"점검 완료 - {status}")


# 매일 오전 9시 실행
schedule.every().day.at("09:00").do(run_checks)

logger.info("스케줄러 시작 - 매일 09:00 점검")

# 시작 시 즉시 한 번 테스트 전송
send_telegram("✅ 모의투자 점검 스케줄러 시작됨\n매일 오전 9시에 결과를 보내드릴게요!")

while True:
    schedule.run_pending()
    time.sleep(30)
