#!/bin/bash
# 정기 점검 — Claude Code 분석 후 텔레그램 전송
# 평일 20:00 저녁 점검 + 월요일 매시간 점검 겸용

WORKDIR="/home/ubuntu/-claude-test-"
TOKEN="8707168013:AAH5yIsoaLoxcA0Lthiw7RaIzD1YcJx8cc8"
CHAT_ID="8448138406"
LOG="$WORKDIR/nohup.out"
NOW_KST=$(TZ=Asia/Seoul date '+%Y-%m-%d %H:%M')

send_tg() {
    curl -s -X POST "https://api.telegram.org/bot${TOKEN}/sendMessage" \
        -d chat_id="$CHAT_ID" \
        -d parse_mode="Markdown" \
        --data-urlencode "text=$1" > /dev/null
}

send_tg "🔍 *[$NOW_KST] 점검 시작...*"

TRADE_LOG=$(grep -E "매수|매도|BUY|SELL|SKIP|ollama|ERROR|리스크|CAUTION|STOP" "$LOG" | tail -50)
ERROR_LOG=$(grep -E "ERROR|Exception|Traceback" "$LOG" | tail -20)
PROC=$(ps aux | grep proxy_v54 | grep -v grep | awk '{print "PID:"$2, "CPU:"$3"%", "MEM:"$4"%"}')
DISK=$(df -h / | tail -1 | awk '{print $3"/"$2, "("$5")"}')
MEM=$(free -h | grep Mem | awk '{print $3"/"$2}')
RISK=$(python3 ~/.openclaw/workspace-trading/scripts/risk_gate.py 2>/dev/null)
PERF=$(python3 "$WORKDIR/performance_tracker.py" --short 2>/dev/null)

PROMPT="현재 시각: $NOW_KST KST
서버 점검 보고서를 작성해줘.

[시스템 상태]
- proxy_v54.py: ${PROC:-실행 안 됨}
- 디스크: $DISK / 메모리: $MEM
- 리스크 게이트: $RISK

[매매 성과]
$PERF

[최근 매매/Ollama 로그]
$TRADE_LOG

[에러 로그]
$ERROR_LOG

아래 형식으로 작성해줘:
1. 현재 상태 요약 (2줄)
2. 문제점 또는 주의사항 (있으면)
3. 개선 제안 (있으면 1~2가지)

간결하게, 텔레그램 메시지 형식으로."

REPORT=$(cd "$WORKDIR" && claude --permission-mode bypassPermissions --print "$PROMPT" 2>/dev/null)

if [ -z "$REPORT" ]; then
    send_tg "⚠️ *[$NOW_KST] Claude Code 응답 없음 — 수동 확인 필요*"
else
    send_tg "📋 *[$NOW_KST] 점검 보고서*

$REPORT"
fi
