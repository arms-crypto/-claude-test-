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
내일 장 시작 전 참고용으로 아래 형식으로 분석해줘.

[스캔 결과]
{result}

[요청 형식]
1. 매수 신호 종목 — 종목명과 신호 수를 명시하고, ⭐(외국인+기관 동시)는 특히 강조. 신호 높은 순으로 나열.
2. 내일 주목 TOP 2~3 — 신호 수 + 누적 일수 + 단타/스윙 기준으로 가장 유망한 종목 선정 이유 설명.
3. 주의 종목 — 매도 신호나 신호 급감 종목 있으면 언급.

종목명과 신호 수를 반드시 숫자로 명시할 것. 추측하지 말고 위 데이터만 기반으로.'''
print(call_mistral_only(prompt, system='한국 주식 트레이딩 전문가. 데이터 기반으로 구체적이고 간결하게 한국어로.'))
" 2>/dev/null)

    if [ -n "$ANALYSIS" ]; then
        send_tg "🤖 [Ollama 분석]

$ANALYSIS"
    fi
fi
