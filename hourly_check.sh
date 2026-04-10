#!/bin/bash
# 매시간 점검 — 결과를 파일에만 저장 (텔레그램 전송 없음)

WORKDIR="/home/ubuntu/-claude-test-"
LOG="$WORKDIR/nohup.out"
REPORT_DIR="$WORKDIR/inspect_reports"
NOW_KST=$(TZ=Asia/Seoul date '+%Y-%m-%d %H:%M')
HOUR=$(TZ=Asia/Seoul date '+%H')
DAILY_FILE="$REPORT_DIR/monday_$(TZ=Asia/Seoul date '+%Y%m%d').txt"

mkdir -p "$REPORT_DIR"

TRADE_LOG=$(grep -E "매수|매도|BUY|SELL|SKIP|ollama|ERROR|CAUTION|STOP" "$LOG" | tail -50)
ERROR_LOG=$(grep -E "ERROR|Exception|Traceback" "$LOG" | tail -20)
PROC=$(ps aux | grep proxy_v54 | grep -v grep | awk '{print "PID:"$2, "CPU:"$3"%", "MEM:"$4"%"}')
RISK=$(python3 ~/.openclaw/workspace-trading/scripts/risk_gate.py 2>/dev/null)

PROMPT="현재 시각: $NOW_KST KST — 시간별 점검 결과를 간결하게 기록해줘.

[시스템]
- proxy_v54.py: ${PROC:-실행 안 됨}
- 리스크 게이트: $RISK

[최근 매매/Ollama 로그]
$TRADE_LOG

[에러 로그]
$ERROR_LOG

형식:
[$NOW_KST] 상태: 정상/주의/이상
- 주요 내용 1~3줄
- 문제 있으면 명시, 없으면 '특이사항 없음'"

RESULT=$(cd "$WORKDIR" && claude --permission-mode bypassPermissions --print "$PROMPT" 2>/dev/null)

echo "" >> "$DAILY_FILE"
echo "=== $NOW_KST ===" >> "$DAILY_FILE"
echo "${RESULT:-응답 없음}" >> "$DAILY_FILE"
echo "점검 완료: $NOW_KST → $DAILY_FILE"
