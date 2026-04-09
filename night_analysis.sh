#!/bin/bash
# 평일 20:35 — 장 마감 후 워치리스트 차트 분석 → 텔레그램 전송 + RAG 저장 + Ollama 분석

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

# 슬립 타이머 리셋 — 스캔 중 Ollama 미호출로 PC 절전 방지
curl -s http://localhost:11435/ping_sleep_timer > /dev/null

RESULT=$(cd "$WORKDIR" && timeout 600 python3 -c "
from auto_trader import scan_buy_signals_for_chat
print(scan_buy_signals_for_chat(months=3))
" 2>/dev/null)

if [ -z "$RESULT" ]; then
    send_tg "⚠️ [$NOW_KST] 분석 실패 — 서버 상태 확인 필요"
else
    # RAG scan_memory에 저장 (장중 Ollama 참조용)
    cd "$WORKDIR" && python3 -c "
from rag_store import store_scan_result
store_scan_result('''$RESULT''', period_label='3개월')
" 2>/dev/null

    # 내일 분석 준비 — 매수 신호 종목 주가 + 포트폴리오 + 시장전망 (압축)
    PREP=$(cd "$WORKDIR" && timeout 300 python3 -c "
import re
from llm_client import call_mistral_only
from search_utils import search_and_summarize

result = '''$RESULT'''
buy_section = result.split('⏸')[0] if '⏸' in result else result
codes = re.findall(r'\((\d{6})\)', buy_section)
codes = list(dict.fromkeys(codes))[:5]

parts = []

# 매수신호 종목 현재가
if codes:
    from mock_trading.kis_client import get_best_price
    lines = []
    for code in codes:
        try:
            price = get_best_price(code) or 0
            lines.append(f'{code}:{price:,}원')
        except Exception:
            pass
    if lines:
        parts.append('📈 ' + '  '.join(lines))

# 포트폴리오 한줄 요약
try:
    from llm_client import _execute_tool_call
    pf = _execute_tool_call('query_portfolio', {})
    if pf and len(pf) > 10:
        parts.append(f'💼 {pf[:200]}')
except Exception:
    pass

# 내일 시장 전망 2줄
try:
    market = search_and_summarize('내일 한국 증시 전망 코스피')
    if market and len(market) > 20:
        summary = call_mistral_only(
            f'다음 시장 정보를 2줄로 요약:\n{market[:500]}',
            system='증시 전문가. 핵심만 2줄.'
        )
        parts.append(f'🌐 {summary}')
except Exception:
    pass

print('\n'.join(parts))
" 2>/dev/null)

    # 스캔 결과 + 준비 데이터 합쳐서 전송 (4000자 초과 시 분리)
    FOOTER="
📅 $NOW_KST KST"
    COMBINED="🌙 [내일 참고] 외국인+기관 워치리스트 분석

$RESULT
${PREP:+
─────────────────
📋 내일 분석 준비
$PREP}$FOOTER"

    if [ ${#COMBINED} -le 4000 ]; then
        send_tg "$COMBINED"
    else
        # 스캔 결과 자체가 길면 앞부분만 전송 (Python으로 한글 멀티바이트 안전 처리)
        MAX_LEN=3900
        RESULT_SHORT=$(python3 -c "
import sys
s = sys.stdin.read()
print(s[:${MAX_LEN}] + ('\n...(생략)' if len(s) > ${MAX_LEN} else ''))
" <<< "$RESULT")
        send_tg "🌙 [내일 참고] 외국인+기관 워치리스트 분석

$RESULT_SHORT$FOOTER"
        [ -n "$PREP" ] && send_tg "📋 [내일 분석 준비]
$PREP"
    fi

    # Ollama 분석 요약 (별도 메시지)
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
