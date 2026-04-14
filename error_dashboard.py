#!/usr/bin/env python3
"""
🎯 에러 모니터 대시보드
- 웹 UI (Flask)
- JSON 상태 파일 (자동 갱신)
- 로그 로테이션 (7일 자동 삭제)
"""

import os
import json
import time
import gzip
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict
import re
from flask import Flask, jsonify, render_template_string

app = Flask(__name__)

# 설정
CONFIG = {
    'log_file': '/home/ubuntu/-claude-test-/proxy_v54.log',
    'status_file': '/home/ubuntu/-claude-test-/status.json',
    'dashboard_port': 11436,
    'retention_days': 7,
}

class StatusTracker:
    """상태 추적 및 통계"""
    def __init__(self):
        self.errors = defaultdict(int)
        self.last_updated = None
        self.uptime_start = datetime.now()

    def update_from_log(self):
        """로그에서 에러 통계 업데이트"""
        log_path = Path(CONFIG['log_file'])
        if not log_path.exists():
            return False

        self.errors.clear()
        # error_monitor.py와 동기화된 패턴 (CLAUDE.md 참조)
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

        try:
            with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
                lines = f.readlines()[-5000:]  # 최근 5000줄

            for line in lines:
                for error_type, pattern in error_patterns.items():
                    if re.search(pattern, line, re.IGNORECASE):
                        self.errors[error_type] += 1
                        break
        except Exception as e:
            print(f"로그 업데이트 실패: {e}")
            return False

        self.last_updated = datetime.now()
        return True

    def to_dict(self):
        """JSON 직렬화"""
        now = datetime.now()
        uptime = (now - self.uptime_start).total_seconds()

        return {
            'timestamp': now.isoformat(),
            'uptime_seconds': uptime,
            'errors': dict(self.errors),
            'total_errors': sum(self.errors.values()),
            'status': 'healthy' if sum(self.errors.values()) < 50 else 'warning' if sum(self.errors.values()) < 200 else 'critical',
        }

tracker = StatusTracker()

def rotate_logs():
    """로그 로테이션 (7일 이상 된 파일 삭제)"""
    log_dir = Path(CONFIG['log_file']).parent
    cutoff = datetime.now() - timedelta(days=CONFIG['retention_days'])

    for log_file in log_dir.glob('error_monitor.log*'):
        try:
            if log_file.stat().st_mtime < cutoff.timestamp():
                log_file.unlink()
                print(f"🗑️ 오래된 로그 삭제: {log_file.name}")
        except Exception as e:
            print(f"로그 삭제 실패: {e}")

def save_status():
    """상태를 JSON 파일로 저장"""
    try:
        with open(CONFIG['status_file'], 'w', encoding='utf-8') as f:
            json.dump(tracker.to_dict(), f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"상태 저장 실패: {e}")

# =============== Flask 엔드포인트 ===============

HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>🚨 에러 모니터 대시보드</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Segoe UI', sans-serif;
            background: #0f172a;
            color: #e2e8f0;
            padding: 20px;
        }
        .container { max-width: 1200px; margin: 0 auto; }
        h1 {
            margin-bottom: 30px;
            font-size: 2em;
            background: linear-gradient(135deg, #3b82f6, #8b5cf6);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }
        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }
        .card {
            background: #1e293b;
            border: 1px solid #334155;
            border-radius: 8px;
            padding: 20px;
            transition: all 0.3s;
        }
        .card:hover { border-color: #64748b; box-shadow: 0 0 20px rgba(59, 130, 246, 0.2); }

        .stat-box {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 15px;
            background: #0f172a;
            border-radius: 6px;
            margin: 10px 0;
        }
        .stat-label { font-size: 0.9em; color: #94a3b8; }
        .stat-value { font-size: 2em; font-weight: bold; }

        .status-healthy { color: #10b981; }
        .status-warning { color: #f59e0b; }
        .status-critical { color: #ef4444; }

        .error-list {
            max-height: 300px;
            overflow-y: auto;
        }
        .error-item {
            padding: 10px;
            margin: 5px 0;
            background: #0f172a;
            border-left: 3px solid #ef4444;
            border-radius: 4px;
            font-family: monospace;
            font-size: 0.9em;
        }

        .chart { height: 200px; position: relative; }
        .bar {
            display: flex;
            align-items: center;
            margin: 10px 0;
        }
        .bar-label { width: 120px; font-size: 0.9em; }
        .bar-container {
            flex: 1;
            height: 25px;
            background: #334155;
            border-radius: 4px;
            overflow: hidden;
            margin: 0 10px;
        }
        .bar-fill {
            height: 100%;
            background: linear-gradient(90deg, #3b82f6, #8b5cf6);
            transition: width 0.3s;
        }
        .bar-value { width: 40px; text-align: right; font-weight: bold; }

        .footer {
            text-align: center;
            color: #64748b;
            margin-top: 30px;
            font-size: 0.9em;
        }

        .refresh-info {
            text-align: center;
            color: #64748b;
            margin: 20px 0;
            font-size: 0.85em;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>🚨 에러 모니터 대시보드</h1>

        <div class="grid">
            <!-- 상태 요약 -->
            <div class="card">
                <h2>📊 현재 상태</h2>
                <div class="stat-box">
                    <div class="stat-label">상태</div>
                    <div class="stat-value status-{{ status }}">{{ status | upper }}</div>
                </div>
                <div class="stat-box">
                    <div class="stat-label">총 에러</div>
                    <div class="stat-value">{{ total_errors }}</div>
                </div>
                <div class="stat-box">
                    <div class="stat-label">가동 시간</div>
                    <div class="stat-value">{{ uptime_hours }}h {{ uptime_mins }}m</div>
                </div>
                <div class="stat-box">
                    <div class="stat-label">마지막 갱신</div>
                    <div style="font-size: 0.9em;">{{ last_updated }}</div>
                </div>
            </div>

            <!-- 에러 유형별 분포 -->
            <div class="card">
                <h2>📈 에러 분포</h2>
                <div class="chart">
                    {% for error_type, count in errors.items() %}
                    <div class="bar">
                        <div class="bar-label">{{ error_type }}</div>
                        <div class="bar-container">
                            <div class="bar-fill" style="width: {{ (count / max_errors * 100) | int }}%"></div>
                        </div>
                        <div class="bar-value">{{ count }}</div>
                    </div>
                    {% endfor %}
                </div>
            </div>

            <!-- 시스템 정보 -->
            <div class="card">
                <h2>⚙️ 시스템</h2>
                <div class="stat-box">
                    <div class="stat-label">모니터 포트</div>
                    <div style="font-family: monospace;">{{ monitor_port }}</div>
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
                    <div>7일</div>
                </div>
            </div>
        </div>

        <div class="refresh-info">
            ⏱️ 매 1분마다 자동 갱신 (웹 F5 누르거나 기다리세요)
        </div>

        <div class="footer">
            🚀 에러 모니터 대시보드 v1.0 | 데이터는 7일 후 자동 삭제됩니다
        </div>
    </div>

    <script>
        function refreshData() {
            fetch('/api/status')
                .then(r => r.json())
                .then(data => {
                    location.reload();
                })
                .catch(e => console.error(e));
        }
        // 1분마다 자동 갱신
        setInterval(refreshData, 60000);
    </script>
</body>
</html>
"""

@app.route('/')
def dashboard():
    """메인 대시보드"""
    tracker.update_from_log()
    data = tracker.to_dict()

    uptime_sec = data['uptime_seconds']
    uptime_hours = int(uptime_sec // 3600)
    uptime_mins = int((uptime_sec % 3600) // 60)

    max_errors = max(data['errors'].values()) if data['errors'] else 1

    html = render_template_string(
        HTML_TEMPLATE,
        status=data['status'],
        total_errors=data['total_errors'],
        uptime_hours=uptime_hours,
        uptime_mins=uptime_mins,
        last_updated=data['timestamp'][:16],
        errors=data['errors'],
        max_errors=max_errors,
        monitor_port=CONFIG['dashboard_port'],
    )
    return html

@app.route('/api/status')
def api_status():
    """JSON API"""
    tracker.update_from_log()
    return jsonify(tracker.to_dict())

@app.route('/health')
def health():
    """헬스 체크"""
    return jsonify({'status': 'ok'})

def background_worker():
    """백그라운드 상태 갱신 및 로그 로테이션"""
    while True:
        try:
            # 상태 업데이트
            tracker.update_from_log()
            save_status()

            # 로그 로테이션 (매시간)
            rotate_logs()

            time.sleep(60)  # 1분마다
        except Exception as e:
            print(f"백그라운드 워커 오류: {e}")
            time.sleep(60)

if __name__ == '__main__':
    import threading

    print(f"🚀 에러 대시보드 시작: http://localhost:{CONFIG['dashboard_port']}")
    print(f"📊 상태 파일: {CONFIG['status_file']}")
    print(f"📝 로그 파일: {CONFIG['log_file']}")
    print(f"🗑️ 보관 기간: {CONFIG['retention_days']}일")

    # 백그라운드 워커 시작
    worker = threading.Thread(target=background_worker, daemon=True)
    worker.start()

    # Flask 시작
    app.run(host='0.0.0.0', port=CONFIG['dashboard_port'], debug=False)
