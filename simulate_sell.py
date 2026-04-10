#!/usr/bin/env python3
"""
매도 판단 시뮬레이션 — Ollama check_after 테스트
실제 pykrx 데이터 + 실제 Ollama 호출
"""
import sys, os
sys.path.insert(0, "/home/ubuntu/-claude-test-")
os.chdir("/home/ubuntu/-claude-test-")

import datetime, pytz
from proxy_v54 import _ollama_sell_decision, calculate_chart_signals
from pykrx import stock as pykrx_stock

KST = pytz.timezone("Asia/Seoul")

# ── 시뮬 종목 (실제 종목 + 다양한 시나리오) ──────────────────────────
CASES = [
    # (code, name, avg_price, scenario)
    ("005930", "삼성전자",  72000, "소폭 하락 (-2%), 변동성 보통"),
    ("000660", "SK하이닉스", 180000, "상승 중 (+4%), 고점 근처"),
    ("035420", "NAVER",     180000, "급락 중 (-6%), 고변동성"),
    ("051910", "LG화학",    300000, "횡보 (+0.5%), 낮은 변동성"),
    ("207940", "삼성바이오",  900000, "강한 상승 (+8%), 추세 양호"),
]

def simulate():
    kst_now = datetime.datetime.now(KST)
    today_str = kst_now.strftime("%Y%m%d")
    from_str  = (kst_now - datetime.timedelta(days=5)).strftime("%Y%m%d")

    print(f"\n{'='*65}")
    print(f"  Ollama 매도판단 시뮬레이션  —  {kst_now.strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*65}\n")

    for code, name, avg_price, scenario in CASES:
        print(f"▶ {name}({code})  [{scenario}]")
        print(f"  평균단가: {avg_price:,}원")

        # 실제 현재가 조회
        try:
            df = pykrx_stock.get_market_ohlcv(from_str, today_str, code)
            current = int(df["종가"].iloc[-1])
            day_high = int(df["고가"].iloc[-1])
            day_low  = int(df["저가"].iloc[-1])
            day_range = (day_high - day_low) / day_low * 100
            print(f"  현재가: {current:,}원  "
                  f"(고:{day_high:,} 저:{day_low:,} 변동폭:{day_range:.1f}%)")
        except Exception as e:
            print(f"  ⚠ 현재가 조회 실패: {e} — 평균단가로 대체")
            current = avg_price

        pnl = (current - avg_price) / avg_price * 100
        print(f"  손익: {pnl:+.2f}%")

        # 차트 신호
        sig = calculate_chart_signals(code)
        if sig:
            print(f"  차트: RSI={sig['rsi']} MACD={sig['macd_hist']} "
                  f"ADX={sig.get('adx','?')} PDI={sig.get('pdi','?')} MDI={sig.get('mdi','?')} "
                  f"신호={sig['buy_count']}/4")

        # Ollama 판단
        print(f"  → Ollama 호출 중...", end=" ", flush=True)
        decision = _ollama_sell_decision(code, name, pnl, 10, avg_price, current)

        action      = decision["action"]
        ratio       = decision.get("ratio", 0)
        check_after = decision.get("check_after", 15)
        reason      = decision.get("reason", "")
        next_check  = kst_now + datetime.timedelta(minutes=check_after)

        emoji = {"HOLD": "⏸", "SELL_PARTIAL": "🟡", "SELL_ALL": "🔴"}.get(action, "?")
        print(f"{emoji} {action}")
        if action == "HOLD":
            print(f"  다음확인: {next_check.strftime('%H:%M')} ({check_after}분 후)")
        elif action == "SELL_PARTIAL":
            sell_qty = max(1, int(10 * ratio))
            print(f"  매도수량: {sell_qty}주 ({ratio*100:.0f}%)  "
                  f"다음확인: {next_check.strftime('%H:%M')} ({check_after}분 후)")
        else:
            print(f"  → 전량매도")
        print(f"  이유: {reason}")
        print()

    print(f"{'='*65}")
    print("시뮬레이션 완료")

if __name__ == "__main__":
    simulate()
