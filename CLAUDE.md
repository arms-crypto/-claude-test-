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

## 스캔/신호 시스템 (2026-04-06 확정)

### 신호 구조
- **12신호 스윙**: 월봉4 + 주봉4 + 일봉4 (일목균형표·ADX·RSI·MACD)
- **4신호 단타**: 분봉4 — buy_count에 포함 안 함, "단타타이밍" 참고용만 표시
- **RSI 기준**: `RSI > 50` (강한 추세 종목 포함, 구: 30<RSI<70)
- **BUY**: 신호 ≥ 6/12 | **HOLD**: 4-5/12 | **SELL**: < 4/12

### 워치리스트 구조
- `get_watchlist_from_db()` — 외국인 OR 기관 등장 종목 전체 (최대 ~34개)
- 반환: `(code, name, day_cnt, both)` — `both=True`면 외국인+기관 동시 (⭐ 표시)
- SQL: `GROUP BY ticker, name` + `COUNT(DISTINCT investor_type)` → inv_cnt≥2 이면 both
- 오늘 실시간 스크래핑 데이터도 병합 (DB에 없는 신규 종목 추가)

### pre-injection (ai_chat.py 3-2, 3-3) — 절대 제거 금지
```
"순매수" + 스캔키워드 → scan_buy_signals_for_chat() 직접 호출, Ollama 우회
차트 키워드 → analyze_chart_for_chat() 직접 호출, Ollama 우회
"다시보여/방금분석/아까분석" → 세션 캐시(__last_chart_{session_id})에서 반환
```
- 이 블록 없으면 Ollama가 결과를 재가공해 환각 발생
- 차트 분석 결과는 `config.store[f"__last_chart_{session_id}"]`에 캐시 (서버 재시작 시 초기화)

### 병렬 스캔
- `scan_buy_signals_for_chat()` — `ThreadPoolExecutor(max_workers=6)` 병렬 처리
- 결과 신호 수 내림차순 정렬 후 반환
- **출력 포맷**: 종목당 1줄 요약 (Telegram 4096자 제한 대응)
  - `⭐종목명(코드) N/12 단타M/4 [스윙] 누적X일 — 판단요약`
- **개별 종목 차트 분석** (`analyze_chart_for_chat`)만 상세 6줄 포맷 유지

### Ollama 직접 반환 도구 (llm_client.py)
- `_DIRECT_RETURN_TOOLS = {"scan_buy_signals", "get_watchlist", "analyze_chart"}`
- 이 도구들의 결과는 Ollama 재응답 없이 그대로 반환 (환각 방지)

### Ollama 모델 정체성 (llm_client.py _TOOL_SYSTEM)
- 시스템 프롬프트 첫 줄에 명시: `"나는 Ollama_Agent다. mistral-small3.1:24b 모델 기반"`
- 모델명 질문 시 "mistral-small3.1:24b" 정확히 답변 (Claude/GPT 오답 방지)

### 남은 숙제
- `_ollama_buy_decision()` — 여전히 Ollama 호출 → 신호 수 기반 룰로 대체 고려
- 분봉 데이터: 장 마감 후(15:30~) 불안정 → 시간대별 제외 옵션 검토

### 히스토리 복구
- `_naver_net_buy_list(date_str='YYYYMMDD')` — `&ntp=` 파라미터로 과거 날짜 조회 가능
- `collect_smart_flows` — Oracle DB `mock_smart_flows` 테이블에 6개월 보관

## KIS 실전 API + 가상주문 (2026-04-07 전환)

### 접속 설정 (mock_trading/kis_client.py)
- **실전 API** — `https://openapi.koreainvestment.com:9443`, 계좌 44197559-01
- **REAL_TRADE = False** — 주문은 가상(portfolio.db만 업데이트), True로 바꿔야 실제 체결
- **KRX + NXT** — `get_price()` KRX 시세, `get_nxt_price()` NXT 야간 시세, `get_best_price()` 자동 폴백
- tr_id: 조회 FHKST*, 잔고 TTTC8434R, 매수 TTTC0802U, 매도 TTTC0801U

### 거래시간
- `is_trading_hours()`: **KST 08:00~20:00 평일** (NXT 포함)
- `is_nxt_hours()`: 08:00~09:00, 15:30~20:00 (NXT 단독 구간)

### 자동매매 워치리스트
- `select_volume_smart_chart()`: **DB 3개월 워치리스트 ∩ 거래량TOP20** 우선
- 교집합 없으면 워치리스트 전체 → 오늘 실시간 순매수 2차 폴백

### PC 자동 최대절전 (2026-04-07 추가)
- PC SSH: `ultimate@221.144.111.116:2224` (공유기 포트포워딩)
- `send_sleep(delay_min=10)`: 마지막 Ollama 요청 후 10분 유휴 → `shutdown /h` 전송
- `_sleep_watcher` 스레드: 60초 주기, 거래시간 외 구간에서 자동 호출
- `_last_ollama_request`: `call_mistral_only()` 호출마다 갱신
- **PC 절전 타이머 끔** AC/DC 모두 0 (`powercfg /change standby-timeout-ac 0` + `standby-timeout-dc 0`)
- Windows 작업 스케줄러: `RemoteHibernate` (SYSTEM 권한 `shutdown /h`) — `schtasks /run /tn RemoteHibernate`으로 호출
- `send_sleep()`: `shutdown /h` 직접 → `schtasks /run /tn RemoteHibernate` 변경 (권한 문제 해결)

### 가상 포트폴리오
- 초기 잔고: **1억원**
- DB 초기화: 2026-04-07 (백업: `mock_trading/portfolio_backup_20260407.db`)

## 야간 보고서 파이프라인 (2026-04-07 확정)

### night_analysis.sh — 평일 20:35 자동 실행
1. Python → `scan_buy_signals_for_chat(months=3)` 직접 계산 (Ollama 불필요)
2. 텔레그램 전송 (1줄 요약 포맷)
3. `store_scan_result()` → RAG scan_memory 저장
4. Ollama → 스캔 결과 분석 요약 → 텔레그램 전송

### 크론 스케줄 (KST 기준, TZ=Asia/Seoul)
| 시각 | 작업 |
|------|------|
| 15:10 평일 | `collect_smart` — 기관/외국인 순매수 1차 수집 |
| 18:40 평일 | `collect_smart` — 2차 수집 |
| 20:30 평일 | `collect_smart` — 장 마감 최종 수집 |
| 20:35 평일 | `night_analysis.sh` — 워치리스트 스캔 + 텔레그램 + RAG |

### RAG 스캔 결과 pre-injection (ai_chat.py)
- **21:00 이후** + 스캔 관련 키워드 → `search_scan()` RAG 직접 주입 (도구 호출 없음)
- **장중(~20:00)** → 실시간 `scan_buy_signals` 도구 호출

### KIS 토큰 관리 (kis_client.py)
- `threading.Lock()` — 병렬 스캔 시 동시 발급 방지 (403 오류 해결)
- 유효기간 24시간 캐시, 만료 60초 전 자동 갱신

## 현재 진행 이슈
- **가상주문 실전API** — 실시간 시세로 가상매매 중, `REAL_TRADE=True` 전환 시 실제 체결
- **모듈화 예정** — proxy_v54.py → 16개 파일 분리 (안정화 후 진행)
- **장중 자동점검** — 스케줄 에이전트 trig_01NTvrDUFKtYzoNoHfGPMrTF (3/31 09/11/13/15시 KST)
- **모닝 보수 에이전트** — trig_01Wgb24aru4pDzfzMKY52nx4 (평일 09:00 KST)

## 절대 하지 말 것
- pre-injection 블록(ai_chat.py 3-1, 3-2, 3-3) 제거 금지 — 환각 방지 핵심
- 단일 파일(proxy_v54.py)로 되돌리기 금지 — 8개 모듈로 분리된 상태 유지
- `query_portfolio`에 거래내역 라우팅 금지 — `query_trade_history` 사용
- 분봉 신호를 buy_count(12)에 포함 금지 — 단타타이밍 참고용으로만 표시

## 자주 쓰는 명령
```bash
sudo systemctl restart proxy_v54   # 서버 재시작
systemctl status proxy_v54         # 상태 확인
journalctl -u proxy_v54 -n 50      # 최근 로그
grep "tool call" proxy_v54.log | tail -20  # 도구 호출 확인
curl -s http://localhost:11435/health  # 헬스체크
curl -s http://221.144.111.116:11434/api/tags  # PC Ollama 확인
```
