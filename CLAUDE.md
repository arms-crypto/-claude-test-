# 프로젝트 컨텍스트

## 핵심 파일
- `proxy_v54.py` — 메인 서버 (Flask 11435, 텔레그램 봇 2개 + 자동매매)
- `mock_trading/kis_client.py` — KIS API 모의투자 클라이언트
- `mock_trading/mock_trading.py` — 매수/매도 로직, portfolio.db 관리
- `mock_trading/portfolio.db` — SQLite 포트폴리오
- `evening_report.sh` — 평일 20:00 Claude 분석 → 텔레그램 보고
- `hourly_check.sh` — 시간별 점검 (파일 저장)
- `~/.openclaw/workspace-trading/scripts/risk_gate.py` — VIX 리스크 게이트

## 핵심 설정값
| 항목 | 값 |
|------|----|
| Flask 포트 | 11435 |
| PC Ollama | 221.144.111.116:11434 |
| PC 모델 | mistral-small3.1:24b |
| 로컬 Ollama | localhost:11434 (gemma2:2b) |
| 텔레그램 봇 1 | TOKEN_RAW — Ollama_Agent (handle_tg) |
| 텔레그램 봇 2 | TOKEN_SRV — oracleN_Agent_bot (handle_tg_srv) |
| CHAT_ID | 8448138406 |
| 자동매매 간격 | 30초, 장중 KST만 실행 |

## 구조 요약
```
ask_ai()          ← 텔레그램 봇1 (handle_tg) 메시지 처리
  └─ 주가/검색 키워드 감지 → 도구 호출 → call_qwen(=call_mistral_only)
call_mistral_only() ← PC Ollama mistral-small3.1:24b 호출 (3회 재시도)
  └─ 연결 실패 시 WoL → wait_for_ollama()
handle_tg_srv()   ← 텔레그램 봇2, 슬래시 명령 + 포트폴리오 조회
auto_trade_cycle() ← 30초 루프, risk_gate → select_volume → buy/sell
```

## 현재 진행 이슈
- **모의투자 중** — KIS 실전 키 보관 중, 1개월 검증 후 전환 예정 (2026-05)
- **모듈화 예정** — proxy_v54.py → 16개 파일 분리 (안정화 후 진행)
- **장중 자동점검** — 스케줄 에이전트 trig_01NTvrDUFKtYzoNoHfGPMrTF (3/31 09/11/13/15시 KST)

## 자주 쓰는 명령
```bash
sudo systemctl restart proxy_v54   # 서버 재시작
systemctl status proxy_v54         # 상태 확인
journalctl -u proxy_v54 -n 50      # 최근 로그
curl -s http://localhost:11435/health  # 헬스체크
curl -s http://221.144.111.116:11434/api/tags  # PC Ollama 확인
```
