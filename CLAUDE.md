# 프로젝트 컨텍스트

## 협업 구조 (Claude + Mistral)
- **Mistral (pc_worker.py)** — 읽기 전용 분석 전담: 버그 후보 추출, 코드 리뷰, 리서치
  - 도구: `read_file`, `bash(조회)`, `report` 만 허용
  - 실행: `python3 pc_worker.py "분석 지시"` → `/tmp/pc_worker_last_report.txt` 결과 저장
- **Claude** — 보고받은 내용 검토 후 실제 파일 수정

## 핵심 파일
- `proxy_v54.py` — 메인 서버 (Flask 11435, 텔레그램 봇 2개 + 자동매매)
- `ai_chat.py` — ask_ai() 핵심 로직 (봇1 메시지 처리)
- `llm_client.py` — LLM 호출, 도구 정의, WoL
- `telegram_bots.py` — 텔레그램 봇 핸들러, 시장보고서 읽기
- `search_utils.py` — SearXNG / Perplexica 검색
- `mock_trading/kis_client.py` — KIS 가상주문 클라이언트 (계좌 44197559-01, REAL_TRADE=False)
- `mock_trading/kis_client_ky.py` — KIS 실전 클라이언트 (KY 계좌 44384407-01, REAL_TRADE=True)
- `mock_trading/mock_trading.py` — 매수/매도 로직, portfolio.db 관리
- `mock_trading/portfolio.db` — SQLite 가상 포트폴리오 (초기잔고 1억, 2026-04-07 초기화)
- `mock_trading/portfolio_ky.db` — KY 실전 포트폴리오
- `evening_report.sh` — 평일 20:00 Claude 분석 → 텔레그램 보고
- `hourly_check.sh` — 시간별 점검 + 시장보고서 파일 갱신
- `~/.openclaw/workspace-trading/scripts/risk_gate.py` — VIX 리스크 게이트
- `~/.openclaw/workspace-research/data/market_report.txt` — 시장보고서 파일

## 핵심 설정값
| 항목 | 값 |
|------|----|
| Flask 포트 | 11435 |
| PC LM Studio | 221.144.111.116:11434 |
| PC 모델 | google_gemma-4-26b-a4b-it |
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
       → PC LM Studio가 native tool calling으로 도구 스스로 호출

call_mistral_only() ← PC LM Studio google_gemma-4-26b-a4b-it (native tool calling)
  └─ 연결 실패 시 WoL → wait_for_ollama()

call_gemma3()       ← 로컬 Ollama gemma3:4b (프롬프트 기반 tool calling)
  └─ 도구 1회 호출 후 결과 요약 → 텔레그램 전송

handle_tg_srv()     ← 텔레그램 봇2, 슬래시 명령 + call_gemma3
auto_trade_cycle()  ← 30초 루프, risk_gate → select_volume → buy/sell (가상+KY 미러)
```

## 🎯 Python + PC LLM 협업 구조 (2026-04-14 신규)

**핵심 3가지 원칙:**
```
1️⃣ PC 부하 최소화 (신호 ±2 이상만 호출, 비동기)
2️⃣ PC는 Python에 신호 조절 (min_signal 제안)
3️⃣ Python은 PC의 학습데이터로만 활용 (제안 반영)
```

**동작 흐름 (단순함):**
```
Python (자동매매 — 30초마다)
  ├─ 신호 감시 & 매매 실행 (즉시)
  ├─ 손절/익절 자동 처리
  └─ 신호 변화 감지 (±2 이상)
       ↓
PC LLM (숫자만 반환 — 비동기)
  └─ analyze_signal_shift() → 5 (이것만!)
       ↓
Python (학습 & 파라미터 조정)
  ├─ 받은 min_signal=5를 학습데이터로 축적
  ├─ 매달 1회 리뷰해서 sector_params에 적용
  └─ 다음 달부터 조정된 값 사용
```

**예시:**
```
신호 변화: 3/12 → 7/12 (강한 상승)
  ↓
PC 분석: "이건 min_signal=5로 충분해"
  ↓
Python: "학습! min_signal=5를 기억해둘게"
  ↓
(매달 리뷰)
  ↓
Python: sector_params["반도체"]["min_signal"] = 5 적용
  ↓
다음달부터: 반도체는 신호 5개 이상이면 매수
```

**요점:**
- ✅ PC는 숫자 하나만 (4~7) → 단순함
- ✅ Python이 학습데이터로 축적 → 매달 리뷰 적용
- ✅ PC 없으면 Python 매매 정상 (독립적)
- ✅ 점점 더 최적화됨

**daily_strategy.json 구조:**
```json
{
  "date": "2026-04-14",
  "status": "ready",
  "focus_sectors": ["반도체", "에너지"],
  "min_signal_override": {"반도체": 6, "에너지": 7},
  "risk_level": "normal",  // low(신호≥7) / normal(신호≥6) / high(신호≥5)
  "max_holdings": 7,
  "notes": "전략 설명"
}
```

**PC 디렉터 시작:**
```bash
python3 pc_director.py  # 백그라운드 스레드 실행
# 또는 테스트:
python3 pc_director.py test  # 당일 전략 JSON 출력
```

### 월간 리뷰 프로세스 (신뢰도 검증 포함)

**학습데이터 흐름:**
1. **장중** (30초마다) — `auto_trader.py`
   - 보유 종목 신호 변화 감시 (±2 이상)
   - PC LLM 호출 (비동기) → min_signal 제안
   - **신호 조합 분석** (월봉/주봉/일봉 강도)
   - 결과 저장: `pc_learning_history.json`
   
   ```json
   {
     "code": "005930",
     "name": "삼성전자",
     "date": "2026-04-14",
     "signal_shift": "3→7 (+4)",
     "pc_min_signal_suggestion": 5,
     "signal_combo": "strong/strong/weak",
     "signal_strengths": {
       "monthly": 4,
       "weekly": 3,
       "daily": 2
     },
     "timestamp": "2026-04-14T14:23:45"
   }
   ```

2. **월 1회** — 신뢰도 검증 기반 적용 (수동 또는 스케줄)
   - `sector_params.monthly_review()` 호출
   - 학습데이터 분석:
     1. 업종별 평균 min_signal 계산
     2. **신호 조합 신뢰도 점수 계산**
     3. 신뢰도 기반 필터링 규칙 적용

**신뢰도 기반 적용 규칙:**

| 신뢰도 | 조건 | 적용 판단 | 예시 |
|--------|------|---------|------|
| **>70점** | 변화량 ≥1 | ✅ 즉시 적용 | strong/strong/weak (71점) |
| **60-70점** | 변화량 ≥2 | ⚠️ 신중 적용 | strong/strong/strong (66점) |
| **<60점** | 변화량 ≥2.5 | ❌ 매우 신중 | weak/strong/strong (57점) |

**신호 조합 신뢰도 맵 (백테스팅 기반):**

```
strong/strong/weak   → 71점 (가장 신뢰할만함)
strong/strong/strong → 66점
strong/weak/strong   → 66점
strong/weak/weak     → 65점
weak/strong/weak     → 66점
weak/weak/strong     → 66점
weak/weak/weak       → 61점
weak/strong/strong   → 57점 (주의 필요)
```

**수동 월간 리뷰 명령:**
```bash
python3 -c "from sector_params import monthly_review; print(monthly_review())"
```

**리뷰 결과 예시 (신뢰도 적용):**
```json
{
  "status": "completed",
  "reviewed_sectors": {
    "반도체": {
      "sample_count": 5,
      "avg_suggestion": 4.2,
      "avg_reliability": 71,
      "suggested_min_signal": 4,
      "current_min_signal": 6,
      "difference": -2,
      "applied": true,
      "reason": "높은 신뢰도(>70) → 즉시 적용"
    }
  },
  "applied": 1,
  "reliability_filters": {
    "high": 1,
    "medium": 0,
    "low": 0
  }
}
```

## RAG 1단계: 도구 정의 15개 (읽기 전용 주입)
ai_chat.py → call_mistral_only()에서 자동 주입 (llm_client.py RAG 1단계):
1. `get_stock_price` — 주가/시세 조회
2. `web_search` — 웹 검색 (최근 뉴스)
3. `search_local_knowledge` — RAG 검색 (저장된 뉴스/시장보고서)
4. `query_portfolio` — 보유종목/잔고/평가손익 (읽기만)
5. `query_trade_history` — 거래내역/매매기록
6. `get_news` — 종목별 뉴스
7. `deep_search` — 심층분석 (Perplexica)
8. `fetch_url` — URL 읽기
9. `scan_buy_signals` — 매수신호 스캔
10. `get_watchlist` — 워치리스트 조회
11. `analyze_chart` — 차트 기술분석
12. `get_foreign_net_buy` — 외국인 순매수
13. `read_file` — 파일 읽기
14. `write_file` — 파일 쓰기 (관리자만)
15. `run_command` — 쉘 명령 (관리자만)

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

## 스캔/신호 시스템

### 신호 구조
- **12신호 스윙**: 월봉4 + 주봉4 + 일봉4 (일목균형표·ADX·RSI·MACD)
- **4신호 단타**: 분봉4 — buy_count에 포함 안 함, "단타타이밍" 참고용만 표시
- **RSI 기준**: `RSI > 50` (강한 추세 종목 포함)
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
- 시스템 프롬프트 첫 줄에 명시: `"나는 Ollama_Agent다. google_gemma-4-26b-a4b-it 모델 기반"`
- 모델명 질문 시 "google_gemma-4-26b-a4b-it" 정확히 답변 (Claude/GPT 오답 방지)

### 남은 숙제
- `_ollama_buy_decision()` — 여전히 Ollama 호출 → 신호 수 기반 룰로 대체 고려
- 분봉 데이터: 장 마감 후(15:30~) 불안정 → 시간대별 제외 옵션 검토

## KIS 계좌 구성

### 가상주문 계좌 (mock_trading/kis_client.py)
- **실전 API** — `https://openapi.koreainvestment.com:9443`, 계좌 `44197559-01`
- **REAL_TRADE = False** — portfolio.db만 업데이트, 실제 체결 없음
- **KRX + NXT** — `get_price()` KRX, `get_nxt_price()` NXT, `get_best_price()` 자동 폴백
- tr_id: 조회 FHKST*, 잔고 TTTC8434R, 매수 TTTC0802U, 매도 TTTC0801U

### KY 실전 계좌 (mock_trading/kis_client_ky.py) — 2026-04-11 추가
- **실전 API** — 계좌 `44384407-01`
- **REAL_TRADE = True** — 실제 체결, portfolio_ky.db 별도 관리
- **미러 매매** — auto_trade_cycle()에서 가상계좌와 동시 주문, 텔레그램 별도 알림

### 거래시간
- `is_trading_hours()`: **KST 08:00~20:00 평일** (NXT 마감 20:00 기준)
- `is_nxt_hours()`: 08:00~09:00, 15:30~20:00 (NXT 단독 구간)

### 종목 선발 (select_volume_smart_chart)
- 워치리스트 전체 병렬 스캔 → buy_count ≥ 6 내림차순 → 상위 7개
- `ThreadPoolExecutor(max_workers=10)` (~15초)

### PC 자동 최대절전 (llm_client.py)
- `send_sleep()`: LM Studio 유휴 10분 **AND** Windows 사용자 유휴 15분 이상 동시 충족 시만 절전
- `schtasks /run /tn RemoteHibernate` — SYSTEM 권한 절전 실행
- `_get_pc_user_idle_min()` — SSH `quser` cp949 디코딩 + 패턴 파싱

## 야간 보고서 파이프라인

### 크론 스케줄 (KST 기준, TZ=Asia/Seoul)
| 시각 | 작업 |
|------|------|
| 15:10 평일 | `collect_smart` — 기관/외국인 순매수 1차 수집 |
| 18:40 평일 | `collect_smart` — 2차 수집 |
| 20:30 평일 | `collect_smart` — 장 마감 최종 수집 |
| 20:35 평일 | `night_analysis.sh` — 워치리스트 스캔 + 텔레그램 + RAG |

### RAG 스캔 결과 pre-injection (ai_chat.py)
- **21:00 이후** + 스캔 관련 키워드 → `search_scan()` RAG 직접 주입
- **장중(~20:00)** → 실시간 `scan_buy_signals` 도구 호출

### KIS 토큰 관리 (kis_client.py)
- `threading.Lock()` — 병렬 스캔 시 동시 발급 방지 (403 오류 해결)
- 유효기간 24시간 캐시, 만료 60초 전 자동 갱신
- `.kis_token_cache.json` 파일 캐시 — 서버 재시작 후 재발급 없이 재사용

## 차트 설정 (generate_chart_png — auto_trader.py)

### 패널 구성 (5단)
| 순서 | 내용 |
|------|------|
| 1 | 메인: 종가(빨강) + 일목균형표 + MAC채널 |
| 2 | 거래량: 양봉 빨강 / 음봉 파랑 |
| 3 | ADX |
| 4 | RSI |
| 5 | MACD |

### 오버레이 상세 설정 (HTS 기준)

**a. 일목균형표** — 기준1 / 전환1 / 선행2=2
- 기준선: **녹색** / 선행스팬2: **보라색** (전환선·선행스팬1·후행스팬·구름대 모두 제거)
- `_ichimoku_signal()`: `price >= kijun*0.99` — 기준선(녹색)과 일치

**b. MAC 채널** — 기간5, 상한율10%, 하한율10%
- MAC Upper/Lower: **하늘색** / High MA/Low MA: **주황색**

**c. ADX** — DMI기간3, ADX기간3
- ADX: **녹색** / PDI: **빨강** / MDI: **파랑** / 기준선: 7 (빨강 점선)

**d. MACD** — 단기5, 장기13, Signal6
- MACD: **빨강** / Signal: **녹색** / 기준선: 0

**e. RSI** — 기간6, Signal6
- RSI: **빨강** / Signal: **녹색** / 기준선: 30, 70

## 에러 모니터링 시스템

### 모니터링 파일
- `error_monitor.py` (383줄) — 실시간 로그 감시 + 텔레그램 알림 (cooldown 10분)
- `error_dashboard.py` (366줄) — 웹 UI 대시보드 (localhost:11436) + JSON 상태 파일
- 로그 경로: `/home/ubuntu/-claude-test-/proxy_v54.log` → 1000줄 스캔 (error_monitor), 5000줄 (dashboard)

### 감시 에러 패턴 (통합 목록)
| 에러 유형 | 정규식 | 텔레그램 알림 | 대시보드 표시 |
|----------|-------|-------------|------------|
| **ERROR** | `ERROR\s+(.+)` | ✅ (cooldown 600초) | ✅ |
| **TRACEBACK** | `Traceback \(most recent call last\)` | ✅ | ✅ |
| **EXCEPTION** | `Exception:\s+(.+)` | ✅ | ✅ |
| **TYPEERROR** | `TypeError:\s+(.+)` | ✅ | ✅ |
| **ATTRIBUTEERROR** | `AttributeError:\s+(.+)` | ❌ (dashboard only) | ✅ |
| **JSON_ERROR** | `JSONDecodeError\|Expecting value` | ✅ | ✅ |
| **HTTP_ERROR** | `HTTPError:\s+(.+)` | ✅ | ✅ |
| **CONNECTION_ERROR** | `ConnectionError:\s+(.+)` | ✅ | ✅ |
| **TIMEOUT** | `Timeout\|timeout` | ✅ | ✅ |
| **LOB_BUG** | `'LOB' object is not subscriptable` | ✅ | ✅ |
| **KIS_FAILED** | `KIS.*실패\|KY.*실패` | ❌ (dashboard only) | ✅ |
| **DB_FAILED** | `Oracle.*실패\|ORA-` | ✅ | ✅ |
| **PYKRX_ERROR** | `pykrx` | ❌ (백그라운드 노이즈) | ✅ |

### 새 에러 추가 절차
1. 로그에서 새로운 에러 패턴 발견 → CLAUDE.md 위 표에 추가
2. `error_monitor.py` line 214-227 `error_patterns` 리스트 업데이트
3. `error_dashboard.py` line 43-51 `error_patterns` dict 업데이트
4. 서버 재시작: `sudo systemctl restart proxy_v54 && sudo systemctl restart error_dashboard`

### 대시보드 접근
- URL: `http://localhost:11436`
- 상태 API: `http://localhost:11436/api/status` (JSON)
- 자동 갱신: 60초마다

## 현재 진행 이슈
- **PC LLM 효율화** — 2026-04-14 신규: 신호 변화 감지만 호출 (극도로 효율적)
  - 부하 최소화: 신호 ±2 이상 변화만 호출
  - Python 독립: 신규 매수/매도 PC 미참여
  - 학습데이터: PC가 신호 변화 분석 → 보정값 제시 (비동기)
  - 모니터링: `pc_director.get_pc_stats()` → PC 호출 통계 확인
- **전략 검증** — Python이 PC LLM의 09:00 전략을 따름 (`_validate_trade_with_strategy`)
  - 저장: `daily_strategy.json` (포커스 업종, risk_level, min_signal_override)
- **KY 실전 계좌** — 2026-04-11 추가, 독립 매매 중 (`44384407-01`, REAL_TRADE=True)
- **가상주문** — 실시간 시세로 가상매매 중 (`44197559-01`, REAL_TRADE=False)
- **모듈화 예정** — proxy_v54.py → 16개 파일 분리 (안정화 후 진행)
- **Gemma 4 E2B** — 2026-04-14 로컬 Ollama에 설치 완료 (7.2GB) | gemma3:4b 병행 운영

## 다중 계좌 아키텍처 (2026-04-16 완성)

### 설계 원칙
- 각 계좌는 **완전 독립**: 자신의 예수금으로 매수, 자신의 보유종목으로 매도
- 미러 매매 없음 — 트레이너와 KY는 서로 다른 시점/금액으로 매매 가능
- **확장 가능**: 새 계좌 추가 시 `_ACCOUNTS` 리스트에 항목 1개만 추가

### 계좌 레지스트리 (`auto_trader.py` — `_ACCOUNTS`)
```python
_ACCOUNTS = [
    {
        "id": "trainer", "label": "🔵 트레이너",
        "db_path": "mock_trading/portfolio.db",
        "get_mt": _get_auto_mt,      "notify": _tg_notify,
        "log_attr": "_daily_trade_log",
        "last_trades_attr": "_auto_last_trades",
        "max_slots": 7,
    },
    {
        "id": "ky", "label": "🟡 KY",
        "db_path": "mock_trading/portfolio_ky.db",
        "get_mt": _get_auto_mt_ky,   "notify": _tg_notify_ky,
        "log_attr": "_daily_trade_log_ky",
        "last_trades_attr": "_auto_last_trades_ky",  # config.py에 추가됨
        "max_slots": 7,
    },
]
```

### 공통 헬퍼 함수
| 함수 | 역할 |
|------|------|
| `_sell_for_account(acc, code, qty, reason)` | 계좌별 매도 (트레이너: oracle_pool + 기법신뢰도, KY: 텔레그램 알림) |
| `_buy_for_account(acc, code, amount, sig)` | 계좌별 매수 (트레이너: oracle_pool, KY: 텔레그램 알림) |
| `_smart_buy_amount_for_account(acc, code)` | 계좌별 예수금 ÷ 잔여슬롯 계산 |

### auto_trade_cycle() 흐름
```
select_volume_smart_chart() — 1회만 호출 (비용 큰 연산)
  ↓
for acc in _ACCOUNTS:          ← 계좌 루프
  1. acc["get_mt"]()._get_holdings() — 계좌별 보유종목 조회
  2. 종목별: _ollama_sell_decision(..., last_trades=acc의_last_trades)
  3. 매도 → _sell_for_account(acc, ...)
  4. 신규매수 → _buy_for_account(acc, ...) + _smart_buy_amount_for_account(acc, ...)
```

### 새 계좌 추가 절차
1. `config.py`에 `_auto_last_trades_XXX: dict = {}` 추가
2. `auto_trader.py` `_ACCOUNTS` 리스트에 항목 추가
3. `mock_trading/kis_client_XXX.py` 생성 (KIS 키/계좌 설정)
4. 서비스 재시작

### 절대 하지 말 것 (추가)
- `sell_mock()` 안에 KY 미러 코드 복구 금지 — 독립 계좌 구조 핵심
- `auto_trade_cycle()` 을 단일 계좌 방식으로 되돌리지 말 것

## 절대 하지 말 것
- pre-injection 블록(ai_chat.py 3-1, 3-2, 3-3) 제거 금지 — 환각 방지 핵심
- 단일 파일(proxy_v54.py)로 되돌리기 금지 — 8개 모듈로 분리된 상태 유지
- `query_portfolio`에 거래내역 라우팅 금지 — `query_trade_history` 사용
- 분봉 신호를 buy_count(12)에 포함 금지 — 단타타이밍 참고용으로만 표시
- 일목균형표 파라미터를 표준(9/26/52)으로 되돌리지 말 것 — HTS 설정(전환1/기준1/선행2) 유지
- `scan_buy_signals_for_chat`에서 `_ollama_buy_decision` 복구 금지 — 신호수 룰로 충분
- `kis_client_ky.py`의 REAL_TRADE=True 절대 False로 바꾸지 말 것
- **PC LLM 관리자 구조 제거 금지** — `pc_director.py` 삭제, `_validate_trade_with_strategy()` 제거, `daily_strategy.json` 무시 금지
  - PC LLM이 관리자, Python이 작업자 구조 절대 역전 금지
  - 자율 매매(Python만 판단)로 복구 금지 — 효율성과 전략 일관성 핵심

## ⭐ 필수 수행 사항: 새로운 에러 발견 시 절차

**새로운 종류의 에러가 로그에서 발견되면 즉시 다음 절차를 수행하세요:**

1. **이 파일(CLAUDE.md)의 "에러 모니터링 시스템" → "감시 에러 패턴" 테이블에 추가**
   - 정규식 패턴 정의
   - 텔레그램 알림 여부 결정
   - 예: `| **NEW_ERROR** | r'new_pattern' | ✅ or ❌ | ✅ |`

2. **error_monitor.py (line 213-227) error_patterns 리스트에 추가**
   ```python
   (r'new_pattern', 'NEW_ERROR'),  # 설명
   ```

3. **error_dashboard.py (line 43-56) error_patterns dict에 추가**
   ```python
   'NEW_ERROR': r'new_pattern',
   ```

4. **proxy_v54.py (line 74-86과 259-271) 두 위치 모두에 추가**
   ```python
   'NEW_ERROR': r'new_pattern',
   ```

5. **skip_alert 로직 검토 (error_monitor.py line 244)**
   - 텔레그램 알림 제외할 에러면 추가: `skip_alert = error_type in (..., 'NEW_ERROR')`

6. **서비스 재시작**
   ```bash
   sudo systemctl restart proxy_v54
   ```

**주의:** 한 곳이라도 누락되면 에러 감지가 불완전합니다. 4개 파일 모두 동기화 필수!

## 자주 쓰는 명령
```bash
sudo systemctl restart proxy_v54   # 서버 재시작
systemctl status proxy_v54         # 상태 확인
journalctl -u proxy_v54 -n 50      # 최근 로그
curl -s http://localhost:11435/health  # 헬스체크
curl -s http://221.144.111.116:11434/api/tags  # PC LM Studio 확인
```
