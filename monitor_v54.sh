#!/bin/bash
# proxy_v54.py 3시간 모니터링 스크립트 (개선판)
# 포트 충돌 방지: 기존 서버가 살아있으면 재시작 안함

LOG_FILE="/home/ubuntu/-claude-test-/nohup.out"
REPORT_FILE="/home/ubuntu/-claude-test-/monitor_report.log"
SERVER_DIR="/home/ubuntu/-claude-test-"
START_TIME=$(date +%s)
END_TIME=$((START_TIME + 7200))  # 남은 2시간 (총 3시간 중 1시간 경과)
CHECK_INTERVAL=300  # 5분 = 300초
LAST_LINE=0
CHECK_NUM=0
CYCLE_COUNT=0
ERROR_COUNT=0
TRADE_BUY_COUNT=0
TRADE_SELL_COUNT=0
RESTART_COUNT=0

echo "=======================================" | tee -a "$REPORT_FILE"
echo "모니터링 재시작: $(date '+%Y-%m-%d %H:%M:%S KST')" | tee -a "$REPORT_FILE"
echo "종료 예정: $(date -d '@'$END_TIME '+%Y-%m-%d %H:%M:%S KST')" | tee -a "$REPORT_FILE"
echo "=======================================" | tee -a "$REPORT_FILE"

restart_server() {
    echo "[$(date '+%H:%M:%S')] 서버 재시작 중..." | tee -a "$REPORT_FILE"
    # 포트 사용 중인 프로세스만 종료
    local pids=$(fuser 11435/tcp 2>/dev/null)
    if [ -n "$pids" ]; then
        fuser -k 11435/tcp 2>/dev/null
        sleep 3
    fi
    # 포트 해제 확인 후 시작
    if ! fuser 11435/tcp >/dev/null 2>&1; then
        cd "$SERVER_DIR" && nohup python3 proxy_v54.py >> nohup.out 2>&1 &
        sleep 8
        RESTART_COUNT=$((RESTART_COUNT + 1))
        echo "[$(date '+%H:%M:%S')] 서버 재시작 완료 PID:$! (총 ${RESTART_COUNT}회)" | tee -a "$REPORT_FILE"
    else
        echo "[$(date '+%H:%M:%S')] 포트 여전히 사용중 - 재시작 스킵" | tee -a "$REPORT_FILE"
    fi
}

check_server_alive() {
    local status=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 http://localhost:11435/health 2>/dev/null)
    if [ "$status" == "200" ]; then
        return 0
    else
        return 1
    fi
}

check_logs() {
    local current_lines=$(wc -l < "$LOG_FILE" 2>/dev/null || echo 0)

    if [ "$LAST_LINE" -eq 0 ]; then
        LAST_LINE=$((current_lines > 50 ? current_lines - 50 : 0))
    fi

    if [ "$current_lines" -le "$LAST_LINE" ]; then
        echo "[$(date '+%H:%M:%S')] 새 로그 없음" | tee -a "$REPORT_FILE"
        LAST_LINE=$current_lines
        return
    fi

    local new_logs=$(sed -n "$((LAST_LINE+1)),${current_lines}p" "$LOG_FILE" 2>/dev/null)
    local new_count=$((current_lines - LAST_LINE))

    echo "" | tee -a "$REPORT_FILE"
    echo "--- [$(date '+%H:%M:%S')] 체크 #${CHECK_NUM} (새 로그: ${new_count}줄) ---" | tee -a "$REPORT_FILE"

    # ERROR/Exception/Traceback 체크 (현재 세션 오류만)
    local errors=$(echo "$new_logs" | grep -iE "^2026.*ERROR|^2026.*Exception" | head -10)
    if [ -n "$errors" ]; then
        local new_errors=$(echo "$errors" | wc -l)
        ERROR_COUNT=$((ERROR_COUNT + new_errors))
        echo "[!!! ERROR ${new_errors}건 !!!]" | tee -a "$REPORT_FILE"
        echo "$errors" | tee -a "$REPORT_FILE"
    else
        echo "[오류 없음]" | tee -a "$REPORT_FILE"
    fi

    # Traceback 체크
    local tb=$(echo "$new_logs" | grep -c "Traceback")
    if [ "$tb" -gt 0 ]; then
        echo "[!!! Traceback ${tb}건 발견 !!!]" | tee -a "$REPORT_FILE"
        echo "$new_logs" | grep -A5 "Traceback" | head -20 | tee -a "$REPORT_FILE"
    fi

    # auto_trade_cycle 체크
    local cycles=$(echo "$new_logs" | grep -E "자동매매 [0-9]" | wc -l)
    if [ "$cycles" -gt 0 ]; then
        CYCLE_COUNT=$((CYCLE_COUNT + cycles))
        echo "[자동매매 사이클 ${cycles}회]" | tee -a "$REPORT_FILE"
        echo "$new_logs" | grep -E "자동매매 [0-9]" | tail -2 | tee -a "$REPORT_FILE"
    fi

    # mistral/LLM 분석 체크
    local llm=$(echo "$new_logs" | grep -iE "mistral|LLM|AI 판단|분석 결과" | head -3)
    if [ -n "$llm" ]; then
        echo "[LLM 분석]" | tee -a "$REPORT_FILE"
        echo "$llm" | tee -a "$REPORT_FILE"
    fi

    # 매수 체크
    local buys=$(echo "$new_logs" | grep -iE "매수 완료|BUY.*완료|신규매수:[^ 없음]" | head -5)
    if [ -n "$buys" ]; then
        local nb=$(echo "$buys" | wc -l)
        TRADE_BUY_COUNT=$((TRADE_BUY_COUNT + nb))
        echo "[매수 ${nb}건!]" | tee -a "$REPORT_FILE"
        echo "$buys" | tee -a "$REPORT_FILE"
    fi

    # 매도 체크
    local sells=$(echo "$new_logs" | grep -iE "매도 완료|SELL.*완료" | head -5)
    if [ -n "$sells" ]; then
        local ns=$(echo "$sells" | wc -l)
        TRADE_SELL_COUNT=$((TRADE_SELL_COUNT + ns))
        echo "[매도 ${ns}건!]" | tee -a "$REPORT_FILE"
        echo "$sells" | tee -a "$REPORT_FILE"
    fi

    echo "누적: 사이클=${CYCLE_COUNT}, 오류=${ERROR_COUNT}, 매수=${TRADE_BUY_COUNT}, 매도=${TRADE_SELL_COUNT}, 재시작=${RESTART_COUNT}" | tee -a "$REPORT_FILE"
    LAST_LINE=$current_lines
}

# 메인 루프
while [ $(date +%s) -lt $END_TIME ]; do
    CHECK_NUM=$((CHECK_NUM + 1))

    # 서버 생존 확인 (다운시에만 재시작)
    if ! check_server_alive; then
        echo "[$(date '+%H:%M:%S')] 서버 응답 없음 - 재시작 시도" | tee -a "$REPORT_FILE"
        restart_server
    fi

    check_logs

    CURRENT=$(date +%s)
    REMAINING=$((END_TIME - CURRENT))
    if [ $REMAINING -le 0 ]; then
        break
    fi

    WAIT=$((REMAINING < CHECK_INTERVAL ? REMAINING : CHECK_INTERVAL))
    echo "[$(date '+%H:%M:%S')] 다음 체크: ${WAIT}초 후 (남은: $((REMAINING/60))분)" | tee -a "$REPORT_FILE"
    sleep $WAIT
done

# 최종 리포트
echo "" | tee -a "$REPORT_FILE"
echo "=======================================" | tee -a "$REPORT_FILE"
echo "최종 리포트: $(date '+%Y-%m-%d %H:%M:%S KST')" | tee -a "$REPORT_FILE"
echo "총 체크: ${CHECK_NUM}회 | 사이클: ${CYCLE_COUNT}회 | 오류: ${ERROR_COUNT}건" | tee -a "$REPORT_FILE"
echo "매수: ${TRADE_BUY_COUNT}건 | 매도: ${TRADE_SELL_COUNT}건 | 재시작: ${RESTART_COUNT}회" | tee -a "$REPORT_FILE"
echo "=======================================" | tee -a "$REPORT_FILE"
tail -50 "$LOG_FILE" >> "$REPORT_FILE"
echo "모니터링 완료!" | tee -a "$REPORT_FILE"
