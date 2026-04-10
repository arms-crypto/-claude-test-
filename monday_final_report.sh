#!/bin/bash
# 18:00 — 하루치 점검 결과 취합 후 텔레그램 전송

WORKDIR="/home/ubuntu/-claude-test-"
TOKEN="8707168013:AAH5yIsoaLoxcA0Lthiw7RaIzD1YcJx8cc8"
CHAT_ID="8448138406"
DAILY_FILE="$WORKDIR/inspect_reports/monday_$(TZ=Asia/Seoul date '+%Y%m%d').txt"

send_tg() {
    curl -s -X POST "https://api.telegram.org/bot${TOKEN}/sendMessage" \
        -d chat_id="$CHAT_ID" \
        -d parse_mode="Markdown" \
        --data-urlencode "text=$1" > /dev/null
}

if [ ! -f "$DAILY_FILE" ]; then
    send_tg "⚠️ 점검 파일 없음: $DAILY_FILE"
    exit 1
fi

DAILY_LOG=$(cat "$DAILY_FILE")

PROMPT="아래는 오늘 하루 매시간 점검 결과야.
전체를 종합해서 최종 보고서를 작성해줘.

$DAILY_LOG

형식:
📋 *월요일 첫날 종합 보고서*

1. 전체 요약 (3줄)
2. 발생한 문제 / 해결된 문제
3. 개선하거나 수정해야 할 것
4. 내일 주의사항

간결하게, 텔레그램 형식으로."

REPORT=$(cd "$WORKDIR" && claude --permission-mode bypassPermissions --print "$PROMPT" 2>/dev/null)

send_tg "${REPORT:-⚠️ 보고서 생성 실패 — 수동 확인 필요}"
