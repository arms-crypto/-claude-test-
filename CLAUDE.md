# 프로젝트 컨텍스트

## 핵심 파일
- `proxy_v54.py` — 메인 서버 (Flask 11435, 텔레그램 봇 2개 + 자동매매)
- `ai_chat.py` — ask_ai() 핵심 로직 (봇1 메시지 처리)
- `llm_client.py` — LLM 호출, 도구 정의, WoL
- `telegram_bots.py` — 텔레그램 봇 핸들러, 시장보고서 읽기
- `search_utils.py` — SearXNG / Perplexica 검색
- `mock_trading/kis_client.py` — KIS API 모의투자 클라이언트
- `mock_trading/mock_trading.py` — 매수/매도 로직, portfolio.db 관리
- `mock_trading/portfolio.db` — SQLite 포트폴리오
- `evening_report.sh` — 평일 20:00 Claude 분석 → 텔레그램 보고
- `hourly_check.sh` — 시간별 점검 + 시장보고서 파일 갱신
- `~/.openclaw/workspace-trading/scripts/risk_gate.py` — VIX 리스크 게이트
- `~/.openclaw/workspace-research/data/market_report.txt` — 시장보고서 파일 (hourly_check가 갱신)

## 핵심 설정값
| 항목 | 값 |
|------|----|
| Flask 포트 | 11435 |
| PC Ollama | 221.144.111.116:11434 |
| PC 모델 | mistral-small3.1:24b |
| 로컬 Ollama | localhost:11434 (gemma3:4b) |
| 텔레그램 봇 1 | TOKEN_RAW — Ollama_Agent (handle_tg) |
| 텔레그램 봇 2 | TOKEN_SRV — oracleN_Agent_bot (handle_tg_srv) |
| CHAT_ID | 8448138406 |
| 자동매매 간격 | 30초, 장중 KST만 실행 |

## 구조 요약
```
ask_ai()            ← 텔레그램 봇1 (handle_tg) 메시지 처리
  ├─ 3-1) 키워드 감지 → 시장보고서/DB뉴스 컨텍스트 직접 주입 (pre-injection)
  │     "나스닥/vix/시장보고서/미장" → market_report.txt 읽어 주입
  │     "저장된뉴스/db뉴스" → Oracle DB 뉴스 주입
  └─ call_qwen(=call_mistral_only)에 전달
       → PC Ollama가 native tool calling으로 도구 스스로 호출

call_mistral_only() ← PC Ollama mistral-small3.1:24b (native tool calling)
  └─ 연결 실패 시 WoL → wait_for_ollama()

call_gemma3()       ← 로컬 Ollama gemma3:4b (프롬프트 기반 tool calling)
  └─ 도구 1회 호출 후 결과 요약 → 텔레그램 전송

handle_tg_srv()     ← 텔레그램 봇2, 슬래시 명령 + call_gemma3
auto_trade_cycle()  ← 30초 루프, risk_gate → select_volume → buy/sell
```

## 도구 라우팅 규칙 (llm_client.py _TOOL_SYSTEM)
- **주가/시세** → `get_stock_price`
- **시황/증시/나스닥** → `web_search` or `search_local_knowledge`
- **시장보고서/저장된뉴스/DB** → `search_local_knowledge` (단, pre-injection이 먼저 처리)
- **보유종목/잔고/평가손익** → `query_portfolio` (현재 상태 전용)
- **매매내역/거래기록/거래이력** → `query_trade_history` (query_portfolio 사용 금지)
- **종목뉴스** → `get_news`
- **심층분석** → `deep_search` (Perplexica)
- **URL 읽기** → `fetch_url`
- **절대 금지**: 훈련 데이터로 수치 추측, 사용자에게 도구 선택지 제시

## SearXNG 검색 설정 (search_utils.py)
- `time_range="day"` 우선 → 없으면 `time_range="week"` 폴백
- 쿼리에 날짜 텍스트 추가 금지 (엉뚱한 기사 매칭 원인)

## 시스템 프롬프트 핵심 문구 (llm_client.py)
- "검증된 최신 실시간 데이터를 기반으로 대화형 학습그래프를 완성하세요"
- "절대로 수치를 추측하거나 만들지 마세요"
- "사용자에게 어떤 도구를 쓸지 묻거나 선택지를 제시하지 말 것"

## 현재 진행 이슈
- **모의투자 중** — KIS 실전 키 보관 중, 1개월 검증 후 전환 예정 (2026-05)
- **모듈화 예정** — proxy_v54.py → 16개 파일 분리 (안정화 후 진행)
- **장중 자동점검** — 스케줄 에이전트 trig_01NTvrDUFKtYzoNoHfGPMrTF (3/31 09/11/13/15시 KST)

## 절대 하지 말 것
- pre-injection 블록(ai_chat.py 3-1) 제거 금지 — 시장보고서 품질에 핵심
- 단일 파일(proxy_v54.py)로 되돌리기 금지 — 8개 모듈로 분리된 상태 유지
- `query_portfolio`에 거래내역 라우팅 금지 — `query_trade_history` 사용

## 자주 쓰는 명령
```bash
sudo systemctl restart proxy_v54   # 서버 재시작
systemctl status proxy_v54         # 상태 확인
journalctl -u proxy_v54 -n 50      # 최근 로그
grep "tool call" proxy_v54.log | tail -20  # 도구 호출 확인
curl -s http://localhost:11435/health  # 헬스체크
curl -s http://221.144.111.116:11434/api/tags  # PC Ollama 확인
```
