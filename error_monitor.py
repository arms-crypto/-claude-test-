#!/usr/bin/env python3
"""
🚨 통합 에러 감시 시스템
- 실시간 로그 모니터링
- 시스템 상태 체크
- API 헬스 체크
- 자동 복구 시도
- 텔레그램 알림
"""

import os
import sys
import time
import json
import subprocess
import threading
import requests
import psutil
import logging
from datetime import datetime, timedelta
from pathlib import Path
import re
from collections import defaultdict

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler('/home/ubuntu/-claude-test-/error_monitor.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# 설정값
CONFIG = {
    'log_file': '/home/ubuntu/-claude-test-/proxy_v54.log',
    'check_interval': 15,  # 15초마다 체크
    'error_threshold': 3,  # 3번 연속 에러 시 알림
    'proxy_health_url': 'http://localhost:11435/health',
    'telegram_token': os.getenv('TELEGRAM_TOKEN_RAW'),
    'chat_id': '8448138406',
}

class ErrorTracker:
    """에러 추적 및 중복 제거"""
    def __init__(self):
        self.errors = defaultdict(int)  # 에러별 카운트
        self.last_alert = {}  # 마지막 알림 시간

    def should_alert(self, error_key: str, cooldown_sec: int = 300):
        """중복 알림 방지 (cooldown 동안 같은 에러는 1번만)"""
        now = time.time()
        last = self.last_alert.get(error_key, 0)
        if now - last >= cooldown_sec:
            self.last_alert[error_key] = now
            return True
        return False

    def increment(self, error_key: str):
        self.errors[error_key] += 1

    def reset(self, error_key: str):
        self.errors[error_key] = 0

error_tracker = ErrorTracker()

def send_telegram(message: str, parse_mode: str = "HTML"):
    """텔레그램으로 알림 전송"""
    if not CONFIG['telegram_token']:
        logger.warning("텔레그램 토큰 미설정")
        return False

    try:
        url = f"https://api.telegram.org/bot{CONFIG['telegram_token']}/sendMessage"
        payload = {
            "chat_id": CONFIG['chat_id'],
            "text": message,
            "parse_mode": parse_mode
        }
        r = requests.post(url, json=payload, timeout=5)
        if r.status_code == 200:
            logger.info("📱 텔레그램 알림 전송")
            return True
        else:
            logger.error(f"텔레그램 전송 실패: {r.status_code}")
            return False
    except Exception as e:
        logger.error(f"텔레그램 전송 오류: {e}")
        return False


def check_proxy_health():
    """proxy_v54 헬스 체크"""
    try:
        r = requests.get(CONFIG['proxy_health_url'], timeout=3)
        if r.status_code == 200:
            error_tracker.reset('proxy_down')
            return True
        else:
            logger.error(f"❌ Proxy HTTP {r.status_code}")
            error_tracker.increment('proxy_down')
            if error_tracker.should_alert('proxy_down'):
                send_telegram(f"❌ Proxy 헬스 체크 실패: {r.status_code}")
            return False
    except Exception as e:
        logger.error(f"❌ Proxy 접속 불가: {e}")
        error_tracker.increment('proxy_down')
        if error_tracker.errors['proxy_down'] >= 2:
            if error_tracker.should_alert('proxy_down'):
                send_telegram(f"🔴 Proxy 서버 다운 감지 (재시작 시도...)")
                restart_proxy()
        return False

def restart_proxy():
    """proxy_v54 자동 재시작"""
    try:
        result = subprocess.run(
            ['sudo', 'systemctl', 'restart', 'proxy_v54'],
            timeout=10,
            capture_output=True,
            text=True
        )
        if result.returncode == 0:
            logger.info("✅ Proxy 재시작 완료")
            send_telegram("✅ Proxy 자동 재시작 완료")
            return True
        else:
            logger.error(f"재시작 실패: {result.stderr}")
            send_telegram(f"❌ Proxy 재시작 실패: {result.stderr[:100]}")
            return False
    except Exception as e:
        logger.error(f"재시작 오류: {e}")
        return False

def check_system_resources():
    """시스템 리소스 체크"""
    try:
        # 메모리 사용률
        mem = psutil.virtual_memory()
        if mem.percent > 85:
            logger.warning(f"⚠️ 메모리 부족: {mem.percent}%")
            if error_tracker.should_alert('memory_high'):
                send_telegram(f"⚠️ 메모리 부족: {mem.percent}%")

        # CPU 사용률
        cpu = psutil.cpu_percent(interval=1)
        if cpu > 80:
            logger.warning(f"⚠️ CPU 과부하: {cpu}%")
            if error_tracker.should_alert('cpu_high'):
                send_telegram(f"⚠️ CPU 과부하: {cpu}%")

        # proxy_v54 프로세스 체크
        try:
            result = subprocess.run(
                ['pgrep', '-f', 'proxy_v54.py'],
                capture_output=True,
                text=True
            )
            if result.returncode != 0:
                logger.error("❌ proxy_v54 프로세스 없음")
                if error_tracker.should_alert('proxy_process_missing', 30):
                    send_telegram("🔴 proxy_v54 프로세스 없음 (재시작 시도...)")
                    restart_proxy()
        except Exception as e:
            logger.error(f"프로세스 확인 실패: {e}")

    except Exception as e:
        logger.error(f"리소스 체크 실패: {e}")

def monitor_log_file():
    """proxy_v54.log 실시간 모니터링"""
    log_path = Path(CONFIG['log_file'])
    if not log_path.exists():
        logger.warning(f"로그 파일 없음: {log_path}")
        return

    # 에러 패턴들 (CLAUDE.md 통합 목록과 동기화)
    error_patterns = [
        (r'ERROR\s+(.+)', 'ERROR'),
        (r'Traceback \(most recent call last\)', 'TRACEBACK'),
        (r'Exception:\s+(.+)', 'EXCEPTION'),
        (r'TypeError:\s+(.+)', 'TYPEERROR'),
        (r'AttributeError:\s+(.+)', 'ATTRIBUTEERROR'),
        (r'JSONDecodeError|Expecting value', 'JSON_ERROR'),
        (r'HTTPError:\s+(.+)', 'HTTP_ERROR'),
        (r'ConnectionError:\s+(.+)', 'CONNECTION_ERROR'),
        (r'Timeout:\s+(.+)', 'TIMEOUT'),
        (r"'LOB' object is not subscriptable", 'LOB_BUG'),
        (r'KIS.*실패|KY.*실패', 'KIS_FAILED'),
        (r'Oracle.*실패|ORA-', 'DB_FAILED'),
        (r'pykrx', 'PYKRX_ERROR'),  # 추가: 백그라운드 노이즈
    ]

    try:
        # 마지막 1000줄 읽기
        with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()[-1000:]

        # 에러 검색
        error_found = False
        for line in lines:
            for pattern, error_type in error_patterns:
                if re.search(pattern, line, re.IGNORECASE):
                    timestamp = datetime.now().strftime('%H:%M:%S')
                    msg_preview = line.strip()[:80]
                    logger.warning(f"🔴 [{error_type}] {msg_preview}")

                    # 텔레그램 알림 제외 (대시보드에만 표시): AttributeError, KIS_FAILED, PYKRX_ERROR
                    skip_alert = error_type in ('ATTRIBUTEERROR', 'KIS_FAILED', 'PYKRX_ERROR')

                    if not skip_alert and error_tracker.should_alert(error_type, cooldown_sec=600):
                        send_telegram(
                            f"🔴 <b>[{error_type}]</b>\n"
                            f"<code>{msg_preview}</code>"
                        )
                    error_found = True
                    break

        if not error_found:
            error_tracker.reset('log_errors')

    except Exception as e:
        logger.error(f"로그 모니터링 오류: {e}")

def check_oracle_db():
    """Oracle DB 연결 상태 체크"""
    try:
        from db_utils import get_db_pool
        pool = get_db_pool()
        if not pool:
            logger.error("❌ Oracle DB 풀 생성 실패")
            if error_tracker.should_alert('db_pool_failed'):
                send_telegram("❌ Oracle DB 풀 생성 실패")
            return False

        # 간단한 쿼리 실행
        with pool.acquire() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM dual")
                cur.fetchone()
        error_tracker.reset('db_connection')
        return True
    except Exception as e:
        logger.error(f"❌ Oracle DB 오류: {e}")
        error_tracker.increment('db_connection')
        if error_tracker.errors['db_connection'] >= 2:
            if error_tracker.should_alert('db_connection_error'):
                send_telegram(f"❌ Oracle DB 오류: {str(e)[:80]}")
        return False

def check_auto_trading():
    """자동매매 루프 정상 작동 체크 (proxy_v54.log 확인)"""
    try:
        # proxy_v54.log에서 "자동매매" 관련 최근 로그 확인
        log_path = Path(CONFIG['log_file'])
        if not log_path.exists():
            return False

        with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()[-200:]  # 최근 200줄

        # 마지막 "자동매매 루프" 관련 로그 시간 확인
        last_trading_log = None
        for line in reversed(lines):
            if '자동매매' in line or 'auto_trade' in line.lower():
                last_trading_log = line
                break

        if last_trading_log:
            # 너무 오래된 로그면 경고
            if any(err in last_trading_log for err in ['ERROR', 'Exception', 'Failed', 'error']):
                logger.warning("⚠️ 자동매매 로그에 에러 감지")
                if error_tracker.should_alert('trading_error_in_log'):
                    send_telegram(f"⚠️ 자동매매 에러 로그: {last_trading_log.strip()[:100]}")

        error_tracker.reset('trading_error')
        return True
    except Exception as e:
        logger.warning(f"자동매매 체크 스킵: {e}")
        return True  # 체크 실패해도 에러로 취급 안 함

def generate_status_report():
    """상태 리포트 생성"""
    report = []
    report.append("📊 <b>시스템 상태 리포트</b>")
    report.append(f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    report.append("")

    # 각 체크 결과
    checks = [
        ("Proxy 서버", check_proxy_health()),
        ("Oracle DB", check_oracle_db()),
        ("자동매매", check_auto_trading()),
    ]

    for name, result in checks:
        status = "✅" if result else "❌"
        report.append(f"{status} {name}")

    # 시스템 리소스
    report.append("")
    mem = psutil.virtual_memory()
    cpu = psutil.cpu_percent(interval=0.5)
    report.append(f"💾 메모리: {mem.percent}%")
    report.append(f"⚙️ CPU: {cpu}%")

    return "\n".join(report)

def main_loop():
    """메인 감시 루프"""
    logger.info("🚀 에러 감시 시스템 시작")
    send_telegram("🚀 에러 감시 시스템 시작")

    check_count = 0
    while True:
        try:
            check_count += 1

            # 1차 체크: 빠른 것들 (매번)
            check_proxy_health()
            monitor_log_file()
            check_system_resources()

            # 2차 체크: 느린 것들 (30초마다)
            if check_count % 2 == 0:
                check_oracle_db()
                check_auto_trading()

            # 3차 체크: 상태 리포트 (5분마다)
            if check_count % 20 == 0:
                report = generate_status_report()
                logger.info("\n" + report)
                # 문제 없으면 리포트 생략 (조용한 운영)

            time.sleep(CONFIG['check_interval'])

        except KeyboardInterrupt:
            logger.info("🛑 감시 시스템 종료")
            send_telegram("🛑 에러 감시 시스템 종료")
            break
        except Exception as e:
            logger.error(f"메인 루프 오류: {e}")
            time.sleep(5)

if __name__ == '__main__':
    main_loop()
