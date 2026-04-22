# Trading Rules — SSOT

매수·매도·보정 규칙의 단일 진실 공급원(Single Source of Truth).
코드 변경 시 이 문서도 반드시 함께 업데이트할 것.

---

## 1. 매도 조건 (폴백 규칙 기준)

### 단타 (trade_type = "단타")
| 조건 | 액션 | 비율 | 재확인 |
|------|------|------|--------|
| pnl >= +2% | SELL_ALL | 100% | 즉시 |
| pnl <= -1% | SELL_ALL | 100% | 즉시 |
| 그 외 | HOLD | — | 3분 후 |

### 스윙 (trade_type = "스윙", 기본값)
| 조건 | 액션 | 비율 | 재확인 |
|------|------|------|--------|
| pnl >= +5% | SELL_PARTIAL | 30% | 10분 후 |
| pnl <= -2% | SELL_ALL | 100% | 즉시 |
| 그 외 | HOLD | — | 15분 후 |

### 우선순위 충돌 규칙
- LLM 판단 실패 시 → 위 폴백 규칙 적용
- SELL_PARTIAL 후 잔여 수량 ≤ 1주 → 자동으로 SELL_ALL 전환
- 손절 조건이 익절 조건보다 항상 우선 (pnl <= 손절선 먼저 체크)

### 강제청산 규칙
- 단타 포지션: 평일 15:10 이후 → SELL_ALL 강제
- NXT 시간(08:00~09:00, 15:30~20:00) + 당일 정규장 매수 종목 → 매도 불가 (T+2 미결제)

---

## 2. on_fill 보정 규칙

### 전제
- 매수 주문 직후 DB에 낙관적(Optimistic) 기록 (예상 qty/price)
- WebSocket 체결통보 수신 시 실체결 기준으로 보정

### 보정 공식
```
corrected_qty  = old_qty - expected_qty + fill_qty
corrected_cost = old_qty * old_avg - expected_qty * expected_price + fill_qty * fill_price
corrected_avg  = corrected_cost / corrected_qty
```

### 엣지 케이스
- corrected_qty <= 0 → portfolio에서 DELETE
- fill_qty == 0 또는 fill_price == 0 → 보정 스킵
- pending_orders에 order_no 없음 → 보정 스킵 (외부 주문 무시)
- action != "BUY" → 보정 스킵 (매도 체결통보는 처리 안 함)

---

## 3. sync_with_kis 보정 규칙

장 시작(09:00~09:05) 또는 재시작 시 DB와 KIS 실잔고 동기화.
**KIS를 항상 신뢰 소스(Source of Truth)로 사용.**

| 상태 | 처리 |
|------|------|
| DB에만 있는 종목 | DELETE (미체결/오류로 간주) |
| KIS에만 있는 종목 | INSERT (수동매수 등) |
| 수량 불일치 | KIS qty/avg_price로 UPDATE |
| 수량 일치 | 변경 없음 |

sync 완료 후 `_pending_orders` 전체 초기화.

---

## 4. 포지션/평단 계산 규칙

### 신규 매수 (기존 보유 없음)
```
avg_price = buy_price
qty       = buy_qty
```

### 추가 매수 (기존 보유 있음)
```
new_qty  = old_qty + add_qty
new_avg  = (old_qty * old_avg + add_qty * add_price) / new_qty
```

### 손익률 계산
```
pnl_pct = (current_price - avg_price) / avg_price * 100
```

---

## 5. 매수 금액 배분 규칙

### 슬롯 배분
```
remain_slots  = max(1, max_slots - len(현재보유종목))
effective_cash = cash * 0.7  # 전일 대비 5% 이상 손실 시 보수적 운영
               = cash         # 그 외 정상 운영
amount = int(effective_cash / remain_slots)
amount = clamp(amount, 50_000, 5_000_000)  # 최소 5만 / 최대 500만
```

### 보수적 운영 조건
- `cash < prev_day_cash * 0.95` → `effective_cash = cash * 0.7`

### 하한/상한 보정
- `amount < 1주 가격` → `amount = 1주 가격`으로 상향
- KIS 실제 주문가능금액 < amount → KIS 금액으로 하향 조정
- KIS 주문가능금액 == 0 → 매수 스킵

---

## 6. 매수 신호 기준

### 12신호 시스템 (월봉4 + 주봉4 + 일봉4)
각 타임프레임별 4개 신호: 일목균형표, ADX, RSI, MACD

| 판단 | 조건 |
|------|------|
| BUY | buy_count >= 6/12 |
| HOLD | buy_count 4~5/12 |
| SELL | buy_count < 4/12 |

### 개별 신호 기준
- 일목균형표: `price >= kijun * 0.99`
- ADX: `adx > 7 AND PDI > MDI`
- RSI: `rsi > 50` (6주기)
- MACD: `macd_hist > 0` (단기5, 장기13, signal6)

### 위험도별 매수 최소 신호 수
| risk_level | 최소 buy_count |
|-----------|--------------|
| low | 7/12 |
| normal | 6/12 |
| high | 5/12 |

---

## 7. 변경 이력

| 날짜 | 변경 내용 |
|------|---------|
| 2026-04-22 | 최초 작성 — 현재 코드 기반 추출 |
| 2026-04-22 | 스윙 손절 -3% → -2% 강화 반영 |
