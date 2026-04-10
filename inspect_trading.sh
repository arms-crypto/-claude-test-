#!/bin/bash
# 매매점검 스크립트 — 2026-03-30 월요일 전용
# 사용법: inspect_trading.sh [check_num]
#   check_num 1: 08:00 (오류 분석+수정)
#   check_num 2~7: 10:00~20:00 (개선점 보고서만)

WORKDIR="/home/ubuntu/-claude-test-"
LOGFILE="$WORKDIR/nohup.out"
REPORT_DIR="$WORKDIR/inspect_reports"
CHECK_NUM="${1:-1}"
TS=$(TZ=Asia/Seoul date '+%Y%m%d_%H%M')
REPORT="$REPORT_DIR/report_${TS}.txt"

mkdir -p "$REPORT_DIR"

echo "=== 매매점검 #${CHECK_NUM} — $(TZ=Asia/Seoul date '+%Y-%m-%d %H:%M KST') ===" | tee "$REPORT"
echo "" | tee -a "$REPORT"

# ─── 최근 2시간 로그 수집 ─────────────────────────────────────────────
echo "### 최근 로그 요약" | tee -a "$REPORT"
SINCE=$(TZ=Asia/Seoul date -d "2 hours ago" '+%Y-%m-%d %H:%M' 2>/dev/null || date -v-2H '+%Y-%m-%d %H:%M')
echo "기준 시각: $SINCE 이후" | tee -a "$REPORT"
echo "" | tee -a "$REPORT"

# ─── 오류 수집 ──────────────────────────────────────────────────────
echo "### 오류 목록 (ERROR / Exception / Traceback)" | tee -a "$REPORT"
grep -E "(ERROR|Exception|Traceback|오류)" "$LOGFILE" | tail -50 | tee -a "$REPORT"
echo "" | tee -a "$REPORT"

# ─── 자동매매 동작 요약 ──────────────────────────────────────────────
echo "### 자동매매 동작 (매수/매도)" | tee -a "$REPORT"
grep -E "(매수|매도|BUY|SELL|auto_trade)" "$LOGFILE" | tail -30 | tee -a "$REPORT"
echo "" | tee -a "$REPORT"

# ─── Ollama 판단 요약 ────────────────────────────────────────────────
echo "### Ollama 판단 내역" | tee -a "$REPORT"
grep -E "(ollama|SKIP|BUY_STRONG|HOLD|SELL_ALL|SELL_PARTIAL)" "$LOGFILE" | tail -20 | tee -a "$REPORT"
echo "" | tee -a "$REPORT"

# ─── 포트폴리오 현황 ─────────────────────────────────────────────────
echo "### 포트폴리오 현황" | tee -a "$REPORT"
curl -s http://localhost:11435/mock/status 2>/dev/null | tee -a "$REPORT"
echo "" | tee -a "$REPORT"

# ─── 서버 상태 ──────────────────────────────────────────────────────
echo "### 서버 상태" | tee -a "$REPORT"
curl -s http://localhost:11435/health 2>/dev/null | tee -a "$REPORT"
echo "" | tee -a "$REPORT"

echo "--- 보고서 저장: $REPORT ---"
