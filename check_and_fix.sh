#!/bin/bash
# 모의투자 시스템 자동 점검 및 수정 스크립트
# 실행: bash check_and_fix.sh

LOG_FILE="$HOME/-claude-test-/check_$(date +%Y%m%d_%H%M%S).log"
BASE_DIR="$HOME/-claude-test-"

log() {
  echo "[$(date '+%H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

log "===== 모의투자 시스템 점검 시작 ====="

# 1. Docker 컨테이너 상태 확인
log "--- [1] Docker 컨테이너 상태 ---"
cd "$BASE_DIR"
docker compose ps 2>&1 | tee -a "$LOG_FILE"

# 죽어있는 컨테이너 재시작
DEAD=$(docker compose ps --status exited --quiet 2>/dev/null)
if [ -n "$DEAD" ]; then
  log "비정상 컨테이너 감지 -> 재시작 시도"
  docker compose up -d 2>&1 | tee -a "$LOG_FILE"
  sleep 5
else
  log "컨테이너 정상"
fi

# 2. 프록시 서버 헬스체크
log "--- [2] 프록시 서버 헬스체크 ---"
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:11435/health 2>/dev/null)
if [ "$HTTP_CODE" != "200" ]; then
  log "헬스체크 실패 (HTTP $HTTP_CODE) -> 프록시 재시작"
  pkill -f proxy_v53.py 2>/dev/null
  sleep 2
  nohup python3 "$BASE_DIR/proxy_v53.py" >> "$LOG_FILE" 2>&1 &
  sleep 3
  log "프록시 재시작 완료 (PID: $!)"
else
  log "프록시 정상 (HTTP $HTTP_CODE)"
fi

# 3. /ask 엔드포인트 테스트
log "--- [3] /ask 엔드포인트 테스트 ---"
RESPONSE=$(curl -s -X POST http://localhost:11435/ask \
  -H "Content-Type: application/json" \
  -d '{"message": "삼성전자 오늘 주가 뉴스 검색"}' \
  --max-time 30 2>&1)

if echo "$RESPONSE" | grep -q "error\|Error\|failed" ; then
  log "경고: /ask 응답에 오류 포함"
  log "응답: $RESPONSE"
elif [ -z "$RESPONSE" ]; then
  log "경고: /ask 응답 없음"
else
  log "/ask 정상 응답"
fi

# 4. /search 엔드포인트 테스트
log "--- [4] /search 엔드포인트 테스트 ---"
RESPONSE2=$(curl -s -X POST http://localhost:11435/search \
  -H "Content-Type: application/json" \
  -d '{"query": "코스피 오늘 시황", "mode": "searxng"}' \
  --max-time 30 2>&1)

if [ -z "$RESPONSE2" ]; then
  log "경고: /search 응답 없음"
else
  log "/search 정상 응답"
fi

# 5. 모의투자 모듈 import 테스트
log "--- [5] 모의투자 모듈 테스트 ---"
cd "$BASE_DIR"
python3 -c "
import sys
sys.path.insert(0, '.')
try:
    from mock_trading.mock_trading import MockTrading
    print('OK: mock_trading import 성공')
except Exception as e:
    print(f'ERROR: {e}')
" 2>&1 | tee -a "$LOG_FILE"

log "===== 점검 완료 - 로그: $LOG_FILE ====="
