#!/bin/bash
# 평일 21:00 — 워치리스트 차트 분석 후 내일 참고용 텔레그램 전송

WORKDIR="/home/ubuntu/-claude-test-"
TOKEN="8707168013:AAH5yIsoaLoxcA0Lthiw7RaIzD1YcJx8cc8"
CHAT_ID="8448138406"
NOW_KST=$(TZ=Asia/Seoul date '+%Y-%m-%d %H:%M')

send_tg() {
    curl -s -X POST "https://api.telegram.org/bot${TOKEN}/sendMessage" \
        -d chat_id="$CHAT_ID" \
        --data-urlencode "text=$1" > /dev/null
}

send_tg "📡 [$NOW_KST] 내일 참고용 워치리스트 분석 시작..."

RESULT=$(cd "$WORKDIR" && timeout 600 python3 -c "
from auto_trader import scan_buy_signals_for_chat
print(scan_buy_signals_for_chat(months=3))
" 2>/dev/null)

if [ -z "$RESULT" ]; then
    send_tg "⚠️ [$NOW_KST] 분석 실패 — 서버 상태 확인 필요"
else
    send_tg "🌙 [내일 참고] 외국인+기관 워치리스트 분석

$RESULT

📅 분석시각: $NOW_KST KST
💡 장 시작 전 참고용 — 실제 매매 시 장중 재확인 필요"

    # Ollama에 추가 분석 의뢰
    ANALYSIS=$(cd "$WORKDIR" && timeout 180 python3 -c "
from llm_client import call_mistral_only
result = '''$RESULT'''
prompt = f'''다음은 오늘 외국인+기관 순매수 워치리스트 차트 신호 스캔 결과야.
내일 장 시작 전 참고용으로 핵심만 3~5줄로 요약해줘.
매수 신호 종목 중 특히 주목할 것, 관망/매도 신호 중 주의할 것 위주로.

{result}'''
print(call_mistral_only(prompt, system='주식 트레이딩 전문가. 핵심만 간결하게 한국어로.'))
" 2>/dev/null)

    if [ -n "$ANALYSIS" ]; then
        send_tg "🤖 [Ollama 분석]

$ANALYSIS"
    fi
fi
