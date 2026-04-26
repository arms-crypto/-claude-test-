# 프로젝트 컨텍스트

## 📋 다음 작업 로드맵 (2026-04-26 기준)

### 우선순위별 작업 목록

| 순위 | 작업 | 난이도 | 예상 시간 | 상태 |
|------|------|--------|---------|------|
| **4순위** | Gemma 도구 안정성 보강 (파싱 edge case 추가) | ⭐⭐ | 반나절 | ⏳ 대기 |
| **1순위** | 실거래 성과 추적 (`performance_tracker.py`) | ⭐⭐ | 1~2일 | ✅ 완료 |
| **2순위** | 백테스트 실제 활용 (KIS MCP, 전략 YAML 설계) | ⭐⭐⭐ | 2~3일 | ⏳ 대기 |
| **3순위** | 테스트 2~4주차 (계약 테스트 → 로그 리플레이 → CI) | ⭐⭐⭐⭐ | 1~2주 | ⏳ 대기 |

### 4순위: Gemma 도구 안정성
- 목표: `_parse_text_tool_call` edge case 보완, 도구 루프 안정화
- 현황: 오늘 마크다운 링크 변환 버그 수정, args 파싱 강화 완료
- 남은 것: 파싱 실패 시 자동 재시도 로직, 로그에서 실패 패턴 분석

### 1순위: 실거래 성과 추적
- 목표: `performance_tracker.py` 신규 생성
- 내용: 일별/주별 수익률, 누적 수익, MDD(최대낙폭), 업종별 성과
- 데이터: `mock_trading/portfolio.db` trades 테이블 활용
- 출력: 저녁 보고서(evening_report.sh)에 자동 포함

### 실거래 기준일 (중요)
- **2026-04-24부터 정상 가동** — 이전 데이터(04-21~23)는 코드 수정 과도기로 참고용
- 성과 분석·백테스트 결과 해석 시 04-24 이후 데이터 기준으로 판단할 것
- `performance_tracker.py`도 04-24 이후 필터 옵션 추가 예정

### 2순위: 백테스트 활용
- 목표: 현재 `6/12` 신호 임계값이 최적인지 과거 데이터로 검증
- 방법: KIS 백테스터 MCP (포트 8002, 정상 동작 확인됨)
- 절차: 전략 YAML 설계 → `run_backtest` → 결과 분석 → sector_params 반영
- 참고: `.mcp.json`에 `kis-backtest` MCP 등록됨

### 3순위: 테스트 2~4주차
- 2주차: KIS/Oracle API 응답 스키마 계약 테스트
- 3주차: 실거래 로그 리플레이 자동 검증
- 4주차: CI 파이프라인 (ruff + 타입체크 + 단위 + 계약)
- 현황: 1주차 완료 (43개 단위 테스트, `tests/unit/`)

---

## 협업 구조 (Claude + Qwen) — 역할 분담 원칙

### 역할 분담 (엄격히 준수)
| 역할 | 담당 | 해당 작업 |
|------|------|----------|
| **파일 읽기 / 쓰기 / 수정** | **Qwen** | 코드 수정, 파일 생성, 로그 분석, 버그 수정 |
| **설계 / 아키텍처 / 판단** | **Claude** | 구조 결정, 방향 제시, 검토, git commit |

> **핵심 규칙**: Claude는 파일을 직접 수정하지 않는다. 수정이 필요하면 반드시 Qwen(태스크 서버 8001)에 지시하고 결과를 검토한다.

### Qwen (서버보수에이전트.py)
- LM Studio `qwen3.5-27b-claude-4.6-opus-reasoning-distilled-heretic-v2-i1` (포트 8000)
- worker 봇(@OpenClaz_pc_bot) 토큰으로 텔레그램 수신 — 사용자↔Qwen 대화용
- 도구: `read_file`, `bash(조회)`, `write_file(수정)` 내장 — 제한 없음
- 수정 시 자동 .bak 백업 생성, 변경 내용 보고 (git commit은 Claude가 검토 후 직접)
- 실행: `python3 서버보수에이전트.py` → 태스크 완료 후 `/result` 엔드포인트로 결과 수신
- 종료: `/exit` 입력

### Claude (설계자)
- 설계·아키텍처 결정, Qwen 수정 지시, 결과 검토 후 git commit
- 파일 직접 수정 금지 — Qwen 태스크 서버가 꺼져 있으면 먼저 실행 요청

### OpenClaw
- research/trading 에이전트만 운영 (worker 봇 비활성화됨)

## Qwen 사용 가이드 (서버보수에이전트.py)

### 내부 호출 경로 2가지
| 경로 | 함수 | 도구 루프 | 언제 |
|------|------|---------|------|
| 도구 루프 있음 | `call_qwen()` | ✅ XML 태그 실행됨 | 수정 작업, 코드블록 없는 일반 질문 |
| 도구 루프 없음 | `_call_qwen_direct()` | ❌ 텍스트만 반환 | 파일 첨부된 분석, 코드블록 포함 메시지 |

**핵심**: 메시지에 코드블록(```)이 있으면 `has_code=True` → `_call_qwen_direct()` → 실제 파일 수정 불가

### 수정 작업 지시 규칙
- ✅ **코드블록 없이** 텍스트로만 수정 내용 기술
- ✅ "read_file과 replace_text 도구로 직접 수정해줘" 명시
- ❌ 지시문에 ``` 코드블록 포함 금지 → _call_qwen_direct로 라우팅되어 수정 안 됨

### 코드 리뷰 태스크 지시 규칙 (환각 방지)
Qwen에게 코드 리뷰를 요청할 때 **반드시 설계 컨텍스트를 함께 주입**할 것.
맥락 없이 "버그 찾아줘"만 보내면 False Positive(거짓 양성) 급증.

**태스크 지시 템플릿:**
```
[설계 컨텍스트]
- 이 모듈의 목적: ...
- 의도된 설계 선택: (DB 풀 사이즈 고정, 폴백 없는 에러 전파 등)
- 사용 라이브러리 특성: (oracledb with문 자동 반환 등)

[점검 요청]
파일을 read_file로 직접 읽어서 점검. 항목: 1)... 2)...
with문/try-except로 이미 처리된 항목은 버그로 지적하지 말 것.
```

**2패스 워크플로우 (코드 리뷰 + 일반 작업 공통 원칙):**

코드 리뷰:
- 1패스: "파일을 read_file로 읽고 코드 인용 목록만 출력. 판정 금지."
- 2패스: "1패스 인용 목록을 바탕으로 BUG/IMPROVEMENT/UNCERTAIN 판정만."
- 인용 없는 판정 = 자동 폐기. 파일 미읽음 = INSUFFICIENT_EVIDENCE 출력.

일반 작업 (파일 수정 / DB 수정 / 설정 변경):
- 1패스: "read_file 또는 bash로 현재 상태만 읽고 보고. 수정 금지."
- Claude가 1패스 결과 검토 후 수정 내용 확정
- 2패스: "확인된 내용 기반으로 replace_text 또는 bash로 실제 수정 실행."
- 효과: 잘못된 수정 방지 + Qwen 환각 감소 + Claude 검토 후 승인 구조 유지

**7가지 환각 방지 원칙 (서버보수에이전트.py 시스템 프롬프트에 적용됨):**
0. 상태 머신 → 파일 전체 읽기 전 보고서 출력 금지
1. with/try-except 선확인 → 이미 처리된 코드 버그 지적 금지
2. 호출 스택 검증 → 상위 블록 확인 후 보고 (핵심)
3. 지적 금지/필수 지적 명시 → except:pass(흐름제어)=금지, sqlite3 자원누수=필수
4. Chain of Thought → 결론 전 생명주기·내장함수 동작 먼저 서술
5. 자가 검증 → 버그 목록 재검토 후 False Positive 제거
6. 인용 필수 → [파일경로]+[라인번호]+[코드스니펫] 없으면 환각으로 제외
7. Temperature 0.3 + 파일 2~3개 단위로 쪼개서 요청 (컨텍스트 오버플로 방지)

### 수정 완료 확인 방법
```bash
# .bak 백업 파일 생성 여부 (수정됐으면 반드시 생김)
ls -la /home/ubuntu/-claude-test-/파일명.py.bak

# 수정 내용 grep 확인
grep -n "수정된_키워드" /home/ubuntu/-claude-test-/파일명.py
```

### Claude → Qwen 태스크 서버
```bash
# 태스크 전송
curl -s -X POST http://127.0.0.1:8001/task \
  -H "Content-Type: application/json" \
  -d '{"task": "수정 지시 내용 (코드블록 없이)"}'

# 결과 즉시 조회 (완료됐으면 반환, 없으면 None)
curl -s http://127.0.0.1:8001/result/{task_id}

# 자동 블로킹 대기 (완료될 때까지 최대 600초 대기 후 반환) ← 1:1 협업 핵심
curl -s "http://127.0.0.1:8001/wait/{task_id}?timeout=600"
```

**1:1 자율 협업 루프 (사용자 개입 없음):**
```
이전: Claude → 태스크 전송 → 사용자 "생성 완료" 알림 → Claude 결과 확인
이후: Claude → POST /task → GET /wait/{task_id} 블로킹 → 결과 자동 수신 → 후속 판단
```

### Qwen 타임아웃 설정
- `call_qwen()` / `_call_qwen_direct()` 모두 `timeout=(5, 600)` — 최대 10분
- LM Studio 추론 시간 로그의 87895초 등 비정상 수치는 카운터 버그 — 무시

### Qwen 태스크 장중 주의사항
- **장중(KST 08:00~20:00, 비정규장 포함)**: 긴 파일 수정 태스크 자제
  - auto_trader.py의 PC LLM 호출과 LM Studio(8000) 경합 → 신호 분석 지연 가능
  - 짧은 조회/grep 태스크는 무방
- **안전한 파일 수정 시간**: 20:00 이후

### Qwen 태스크 효율 원칙 (2026-04-19 도출)

**역할 분담 (확정)**

| 역할 | 담당 | 도구 |
|------|------|------|
| 전략·패치 설계·읽기 | **Claude** | `Read` / `Bash(grep)` |
| 파일 수정 실행 | **Qwen** | `write_file` 1회 |
| 변경 검증 | **Qwen** | `bash(grep)` |

**Claude가 하는 것:**
- `Read` / `grep`으로 파일 직접 읽기 (Qwen에게 읽기 위임 금지 — 200줄 제한·LLM 비용 낭비)
- 수정 전략·false positive 기준 결정
- 패치 후 전체 파일 내용 설계

**Qwen이 하는 것:**
- Claude가 설계한 내용을 `write_file` 1회로 원자적 적용
- `bash grep [키워드] 파일명`으로 수정 반영 여부 검증 보고

**수정 태스크 (Qwen에게 write_file 지시)**
```bash
curl -s -X POST http://127.0.0.1:8001/task \
  -H "Content-Type: application/json" \
  -d '{"task": "파일명.py를 write_file로 다음 내용으로 교체해줘: [전체 파일 내용]"}'
```

**검증 태스크 (Qwen에게 grep 지시)**
```bash
curl -s -X POST http://127.0.0.1:8001/task \
  -H "Content-Type: application/json" \
  -d '{"task": "bash로 grep -n [키워드] 파일명.py 실행하고 결과 보고해줘."}'
```

**파일 범위 제한 — 반드시 지킬 것:**
- 태스크 1개 = 파일 **1개** (100% 성공률 보장)
- 2개 이상 → 도구 루프 초과 + Reasoning 모델 환각 급증 (오늘 실패 사례 확인)
- 수정 대상 여러 개 → 태스크 분리해서 순차 전송

**Reasoning 모델(Qwen) 도구 생략 대응책:**
- Qwen은 `<think>` 단계에서 훈련 데이터로 답을 만들고 도구를 생략하는 경향
- 지시문에 반드시 명시: **"read_file 도구 실행 결과를 인용하지 않으면 INSUFFICIENT_EVIDENCE 출력"**
- 라인번호 없는 보고 = 환각 → 자동 폐기, 재전송

**환각 근본 차단 — old 문자열 Claude가 직접 제공 (2026-04-20 확정):**

Qwen의 replace_text 실패 근본 원인: read_file → 기억으로 old 생성(환각) → 불일치 → 수정 실패

**해결책: Claude가 Read로 정확한 코드 읽어서 태스크에 old 통째로 포함**
```
"replace_text 도구로 다음을 교체해줘. read_file 불필요.

old:
[Claude가 Read로 읽어온 정확한 코드]

new:
[수정된 코드]"
```
- Qwen은 replace_text 1번만 실행 → 환각 여지 없음
- Claude가 Read → 설계 → old/new 구성 → Qwen이 실행만

**태스크 실패 패턴 인식 (즉시 재설계):**
| 증상 | 원인 | 대응 |
|------|------|------|
| .bak 파일 없음 | 수정 안 됨 | 태스크 재전송 |
| 라인번호 없는 코드 인용 | 환각 | Claude가 grep 후 재지시 |
| "파일 내용이 없습니다" | 도구 미실행 | "반드시 read_file 먼저" 명시 |
| 도구 루프 초과 (25회) | 파일 너무 많음 | 파일 1개씩 분리 |

### 한글 파일명 주의
- bash `ls` 결과에서 한글 파일명 깨짐 방지: `env LC_ALL=en_US.UTF-8 ls`
- Qwen regex `[\w]+\.py` → 한글 파일명 못 잡음 → `[\w\uAC00-\uD7A3]+\.py` 사용

## 핵심 파일
- `proxy_v54.py` — 메인 서버 (Flask 11435, 텔레그램 봇 2개 + 자동매매)
- `ai_chat.py` — ask_ai() 핵심 로직 (봇1 메시지 처리)
- `llm_client.py` — LLM 호출, 도구 정의, WoL
- `telegram_bots.py` — 텔레그램 봇 핸들러, 시장보고서 읽기
- `search_utils.py` — SearXNG / Perplexica 검색
- `mock_trading/kis_client.py` — KIS 실전 클라이언트 (트레이너 계좌 44197559-01, REAL_TRADE=True) | `get_orderbook()` 호가 조회 포함
- `mock_trading/kis_client_ky.py` — KIS 실전 클라이언트 (KY 계좌 44384407-01, REAL_TRADE=True) | **트레이너 완성 후 복사본**
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
| PC Ollama | 221.144.111.116:8000 (Qwen 태스크 서버) |
| PC 모델 | google_gemma-4-26b-a4b-it |
| 로컬 Ollama | localhost:11434 (qwen2.5:7b — 폴백용, tool calling 불안정) |
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

call_mistral_only() ← PC Ollama google_gemma-4-26b-a4b-it (native tool calling)
  └─ 연결 실패 시 WoL → wait_for_ollama()

call_gemma3()       ← 로컬 Ollama qwen2.5:7b (프롬프트 기반 tool calling)
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

### 알려진 이슈
- 분봉 데이터: 장 마감 후(15:30~) 불안정 → 시간대별 제외 옵션 검토

## KIS 계좌 구성

### 트레이너 실전 계좌 (mock_trading/kis_client.py) — 2026-04-17 실전 전환
- **실전 API** — `https://openapi.koreainvestment.com:9443`, 계좌 `44197559-01`
- **HTS_ID** — `@2930263` (H0STCNI0 WebSocket tr_key)
- **REAL_TRADE = True** — 실제 체결, portfolio.db 관리
- **KRX + NXT** — `get_price()` KRX, `get_nxt_price()` NXT, `get_best_price()` 자동 폴백
- tr_id: 조회 FHKST*, 잔고 TTTC8434R, 매수 TTTC0802U, 매도 TTTC0801U

### KY 실전 계좌 (mock_trading/kis_client_ky.py) — 2026-04-11 추가
- **실전 API** — 계좌 `44384407-01`
- **HTS_ID** — `@2995879` (H0STCNI0 WebSocket tr_key)
- **REAL_TRADE = True** — 실제 체결, portfolio_ky.db 별도 관리
- **독립 매매** — 트레이너와 완전 독립 (동일 신호, 각자 예수금으로 매매)

### kis_client 파일 추가 규칙 (새 계좌 생성 시)
1. **트레이너(kis_client.py) 기준으로 먼저 완성** — 기능 추가/버그픽스 모두 트레이너 파일에서 선행
2. **완성된 트레이너 파일 복사** → `kis_client_XXX.py`
3. **민감 정보만 교체**: APP_KEY, APP_SECRET, ACCOUNT_NO, ACCOUNT_CD, 토큰 캐시 파일명
> 두 파일 동시 수정 금지 — 트레이너 완성 후 복사가 100% 동기화 보장

### 거래시간
- `is_trading_hours()`: **KST 08:00~20:00 평일** (NXT 마감 20:00 기준)
- `is_nxt_hours()`: 08:00~09:00, 15:30~20:00 (NXT 단독 구간)

### 종목 선발 (select_volume_smart_chart)
- 워치리스트 전체 병렬 스캔 → buy_count ≥ 6 내림차순 → 상위 7개
- `ThreadPoolExecutor(max_workers=10)` (~15초)

### PC 자동 최대절전 (llm_client.py)
- `send_sleep()`: LM Studio 유휴 20분 **AND** Windows 사용자 유휴 20분 이상 동시 충족 시만 절전
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

## 현재 운영 상태
- **트레이너 실전** — `44197559-01`, REAL_TRADE=True, 2026-04-17 전환
- **KY 실전 계좌** — `44384407-01`, REAL_TRADE=True, 독립 매매 중

## 미완료 이슈
- **모듈화 예정** — proxy_v54.py → 16개 파일 분리 (안정화 후 진행)

## 테스트 하네스 (2026-04-22 1주차 완료)

### 구조
```
specs/trading-rules.md          — 매수·매도·보정 규칙 SSOT (여기가 정답)
tests/unit/trading_logic.py     — 순수함수 7개 (외부 의존성 없음)
tests/unit/test_trading_logic.py — 단위 테스트 43개 (0.13초)
```

### 실행
```bash
python3 -m pytest tests/unit/  # 핵심 로직 즉시 검증
```

### 순수함수 목록
| 함수 | 역할 |
|------|------|
| `calc_avg_price` | 추가매수 후 평균단가 계산 |
| `calc_pnl_pct` | 손익률(%) 계산 |
| `calc_corrected_position` | on_fill 체결 보정 (낙관→실체결) |
| `reconcile_holdings` | sync_with_kis DB↔KIS 불일치 분석 |
| `fallback_sell_decision` | LLM 실패 시 폴백 매도 규칙 |
| `calc_slot_allocation` | 슬롯 기반 매수 배정금액 |
| `validate_signal_threshold` | PC 전략 신호 임계값 검증 |

### 로드맵
- **1주차 완료** — 순수함수 + 단위 테스트
- **2주차 (다음주)** — KIS/Oracle 응답 스키마 계약 테스트
- **3주차** — 실거래 로그 리플레이 자동 검증
- **4주차** — CI 파이프라인 (ruff + 타입체크 + 단위 + 계약)

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

## 절대 하지 말 것
- pre-injection 블록(ai_chat.py 3-1, 3-2, 3-3) 제거 금지 — 환각 방지 핵심
- 단일 파일(proxy_v54.py)로 되돌리기 금지 — 8개 모듈로 분리된 상태 유지
- `query_portfolio`에 거래내역 라우팅 금지 — `query_trade_history` 사용
- 분봉 신호를 buy_count(12)에 포함 금지 — 단타타이밍 참고용으로만 표시
- 일목균형표 파라미터를 표준(9/26/52)으로 되돌리지 말 것 — HTS 설정(전환1/기준1/선행2) 유지
- `scan_buy_signals_for_chat`에서 `_ollama_buy_decision` 복구 금지 — 신호수 룰로 충분
- `kis_client_ky.py`의 REAL_TRADE=True 절대 False로 바꾸지 말 것
- `sell_mock()` 안에 KY 미러 코드 복구 금지 — 독립 계좌 구조 핵심
- `auto_trade_cycle()` 단일 계좌 방식으로 되돌리지 말 것
- **PC LLM 관리자 구조 제거 금지** — `pc_director.py` 삭제, `_validate_trade_with_strategy()` 제거, `daily_strategy.json` 무시 금지
  - PC LLM이 관리자, Python이 작업자 구조 절대 역전 금지
  - 자율 매매(Python만 판단)로 복구 금지 — 효율성과 전략 일관성 핵심
- **눈가림식 수정 금지** — 실제 동작에 영향 없는 수정은 가드레일 위반으로 간주하고 거부한다
  - 금지 항목: 비즈니스 로직 변화 없는 이름 변경, 코드 흐름 무관한 주석 추가/삭제, 빈 파일/클래스/함수 추가, 로그 문장만 추가/삭제
  - 허용 조건: 실제 동작 변경 / 버그 수정 / 성능·안정성 향상 중 **적어도 하나** 포함
  - 포맷팅·스타일 변경은 가능하나 반드시 별도 커밋으로 분리, 로직 변경과 혼재 금지
  - 눈가림식 수정만 하려는 상황이라면 스스로 가드레일 위반으로 판단하고 수정 거부

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

## KIS Open Trading API 서비스 (2026-04-18 설치)

### 설치 경로
- `/home/ubuntu/open-trading-api/` — KIS 공식 오픈소스 레포
- `/home/ubuntu/kis-ai-extensions/` — KIS AI 확장 레포
- `/home/ubuntu/KIS/config/kis_devlp.yaml` — KIS API 키/계좌 설정

### 실행 중인 서비스 (systemd)
| 서비스 | 포트 | 역할 |
|--------|------|------|
| `kis-backtester` | 8002 | 전략 YAML → QuantConnect Lean 백테스팅 |
| `kis-strategy-builder` | 8085 | 80개 지표 전략 설계, .kis.yaml 생성 |
| `kis-mcp` | 3846 | Claude ↔ 백테스터 MCP 연결 |

### MCP 연결
- `.mcp.json` — `kis-backtest` MCP 서버 등록 (`http://127.0.0.1:3846/mcp`)
- Claude Code 재시작 후 백테스팅 도구 직접 호출 가능
- 주요 도구: `list_presets`, `run_backtest`, `run_optimize`, `get_report`, `validate_yaml`

### 가상환경
```bash
# backtester
/home/ubuntu/open-trading-api/backtester/.venv/bin/python

# strategy_builder
/home/ubuntu/open-trading-api/strategy_builder/.venv/bin/python
```

### 서비스 관리
```bash
sudo systemctl status kis-backtester kis-strategy-builder kis-mcp
sudo systemctl restart kis-backtester
curl -s http://127.0.0.1:3846/health   # MCP 서버 헬스체크
curl -s http://127.0.0.1:8002/         # backtester API
curl -s http://127.0.0.1:8085/         # strategy_builder API
```

### 서버보수에이전트 현재 동작
- `reasoning_effort`, `max_reasoning_tokens` 파라미터 없음 (GGUF 모델 미지원)
- LM Studio 모델 언로드 자동 감지 → WoL + `_wait_for_model(180)` + 재시도
- 태스크 서버: `task-server.service` systemd 등록 — 부팅 시 자동 시작

## Gemma 완전 자율 관리자 (2026-04-25 구현)

### 구조
```
[장전 08:00 / 장후 20:05 — director_scheduler]
  pc_director.system_review(context_label)
    → 포트폴리오 + 당일승률 + 거시지표 6종 + 보유종목 뉴스 + 에러로그(100건) + 현재전략
    → Gemma 호출 → JSON 결정 → _pending_manager_actions 큐 적재

[30초마다 — auto_trade_cycle]
  pc_director.get_pending_actions() 폴링
    → _execute_manager_action(action):
        strategy_update → daily_strategy.json 덮어쓰기
        alerts         → 로그만 (텔레그램 발송 안 함)
        sell_triggers  → 두 계좌 즉시 매도
        param_adjust   → 로그 + 텔레그램

[10분마다 — _news_watch_loop 데몬]
  보유종목 뉴스 수집 → Gemma 악재 판단
    → HIGH/MEDIUM 악재 → 텔레그램 발송
```

### Gemma에게 주어진 도구 (llm_client.py)
- `read_file` — 500줄 + offset 분할 읽기 (최대 500줄/회)
- `write_file` — **완전 차단** (`⛔ write_file 권한 없음` 반환)
- `get_macro_indicators` — 거시지표 6종 실시간 조회
- `run_command`, `web_search`, `get_news` 등 기존 도구 유지

### 승률 개선 (2026-04-25)
- **최소 보유시간 가드**: 스윙 60분 / 단타 10분 이내 매도 판단 차단
- **Gemma 프롬프트**: `보유일수(일)` → `매수 후 경과(분)` 으로 변경
- 효과: 즉각 0% 매도 12건 → 0건, 예상 승률 51% → 78% 복원

### analyze_chart 강화
- `volume_ratio` (거래량/20일평균) + `이격도_ma20/ma60` 추가 계산·전달
- Gemma가 거래량 동반 여부, 이격도 과열 여부 판단 가능

### llm_client.py 자율 에이전트화
- `_TOOL_SYSTEM` 첫 줄: "자율 행동 에이전트" 선언 + 되묻기 금지
- 도구 루프 3 → 6라운드 (복합 체인 대응)
- 단독 호출(round 0)만 `_DIRECT_RETURN_TOOLS` 즉시 반환 → 체인 중엔 Gemma 종합
- Gemma 4 native `<|tool_call>` 따옴표 형식 파싱 개선

### 절대 하지 말 것 추가
- `write_file` 도구를 Gemma(Ollama)에게 다시 열어주지 말 것 — db_utils.py 오염 사례 발생
- `ai_chat.py`의 `_is_news` 하드코딩 형식 블록 복구 금지 — Gemma 자유도 차단
- `_rule_buy_decision`을 `_ollama_buy_decision`으로 되돌리지 말 것 — LLM 호출 없는 룰 기반 함수임을 명시한 이름

## 에러 모니터링 추가 필터 (2026-04-25)
- `Network is unreachable` / `[Errno 101]` / `NewConnectionError` / `Max retries exceeded`
  → Oracle Cloud 순단으로 발생하는 일시적 Telegram API 연결 오류, 코드 버그 아님
  → `error_monitor.py` + `error_dashboard.py` 양쪽 모두 `_TRANSIENT_NOISE` 리스트로 완전 skip 처리

## 자주 쓰는 명령
```bash
sudo systemctl restart proxy_v54        # 서버 재시작
systemctl status proxy_v54              # 상태 확인
journalctl -u proxy_v54 -n 50           # 최근 로그
curl -s http://localhost:11435/health   # 헬스체크
curl -s http://221.144.111.116:8000/v1/models  # PC LM Studio 모델 목록 조회

sudo systemctl restart task-server      # 태스크 서버 재시작
systemctl status task-server            # 태스크 서버 상태
journalctl -u task-server -n 50         # 태스크 서버 로그
curl -s -X POST http://127.0.0.1:8001/task \
  -H "Content-Type: application/json" -d '{"task": "ping"}'  # 태스크 서버 테스트

python3 graphify.py .                   # 코드 그래프 재빌드
```

## Code Graph (Graphify)

### Claude 사용 규칙
1. **먼저 `graphify-out/GRAPH_REPORT.md` 읽기** — 전체 구조 파악 (진입점·위험태그·콜허브)
2. **필요한 파일만 Read** — 그래프에서 찾은 file:line 으로 바로 이동, 전체 스캔 금지
3. **Qwen 태스크 작성 시 file:line 명시 필수**
   - ❌ `"auto_trade_cycle 매도 조건 수정해줘"`
   - ✅ `"auto_trader.py:642 _sell_for_account 함수에서 매도 조건 수정해줘"`
   - graph.json에서 symbol 조회 → file/line/calls 확인 → 태스크에 포함 → Qwen이 read_file 없이 바로 offset으로 이동
4. **한국어 태스크도 aliases로 자동 매핑됨** — 자동매매/봇1/태스크서버/절전/장중판단/주가조회/포트폴리오/히스토리/세션/슬립타이머 등 aliases 38개 등록

### Graphify 벤치마크 결과 (2026-04-19)

| 케이스 | CTX | 노드 | read_file | bash | 합계 | 시간 |
|--------|-----|------|-----------|------|------|------|
| CTX_자동매매 | ✅ | 10 | 0 | 0 | 0 | 22s |
| CTX_절전 | ✅ | 4 | 0 | 0 | 0 | 26s |
| CTX_장중판단 | ✅ | 9 | 0 | 0 | 0 | 22s |
| NOCTX_raw1 (grep) | ❌ | 0 | 0 | 2 | 2 | 44s |
| NOCTX_raw2 | ❌ | 0 | 0 | 0 | 0 | 26s |

- **CTX 있음 평균 도구 호출: 0.0** / CTX 없음: 1.0 → **탐색 비용 절감 ≈1.0회/태스크**
- CTX 없는 케이스에서 Qwen이 grep을 통해 탐색 (44s vs 22s, 2배 더 느림)

### 운영 지침 (벤치마크 기반)
1. **한국어 aliases 사용** — 자동매매/봇1/절전 등 → GRAPH CONTEXT 자동 주입 → 탐색 0회
2. **aliases 미등록 한국어** → GRAPH CONTEXT 없음 → Qwen grep 탐색 필요 → aliases 추가 권장
3. **symbol명 직접 포함** (auto_trade_cycle) → 항상 매칭, 가장 확실
4. **file:line 명시** → read_file 완전 생략 가능 (최적)
5. 장중(08:00~20:00) 긴 수정 태스크 자제 — Qwen 탐색 비용 + 실매매 간섭 위험

### Stale 방지
- `git commit` 후 자동 재빌드 (`post-commit` hook 설치 완료)
- 수동 재빌드: `python3 graphify.py .`
- **stale 판단 기준**: 작업 전 `graphify-out/graph.json`의 `generated_at`이 최근 커밋보다 오래됐으면 재빌드 후 참고
- stale 상태에서 그래프를 그대로 믿으면 잘못된 파일:라인으로 안내될 수 있음 — 의심되면 재빌드 먼저

### 파일 경로
- `graphify-out/GRAPH_REPORT.md` — 253줄 요약 (항상 로드)
- `graphify-out/graph.json` — symbol 중심 전체 인덱스
- `graphify-out/doc/` — 파일별 상세 (필요 시만)
