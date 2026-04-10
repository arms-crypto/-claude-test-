#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
오늘 외국인+기관 순매수 종목 → 차트신호 → Ollama 매수판단 시뮬레이션
실제 매매 없이 결과만 출력
"""
import sys, os
sys.path.insert(0, "/home/ubuntu/-claude-test-")
os.chdir("/home/ubuntu/-claude-test-")

import datetime
import pytz

from auto_trader import (
    _scrape_naver_codes, _get_name_by_code,
    calculate_chart_signals, _ollama_buy_decision, _classify_trade_type,
)

KST = pytz.timezone("Asia/Seoul")

def run():
    now = datetime.datetime.now(KST)
    print(f"\n{'='*65}")
    print(f"  순매수 매수 시뮬레이션  —  {now.strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*65}")

    print("\n📡 외국인/기관 순매수 TOP20 수집 중...")
    foreign = _scrape_naver_codes("9000", limit=20)
    inst    = _scrape_naver_codes("1000", limit=20)
    print(f"  외국인 {len(foreign)}종목 | 기관 {len(inst)}종목")

    # 중복 종목 (둘 다 순매수) 우선, 나머지도 포함
    both   = [c for c in foreign if c in inst]
    only_f = [c for c in foreign if c not in inst]
    only_i = [c for c in inst if c not in foreign]
    candidates = both + only_f + only_i  # 중복우선 정렬

    print(f"  외국인+기관 동시: {len(both)}종목 ★")
    print(f"  외국인만: {len(only_f)}  |  기관만: {len(only_i)}")
    print(f"  분석 대상: {len(candidates)}종목\n")

    results = []
    for i, code in enumerate(candidates[:15]):  # 최대 15종목
        name = _get_name_by_code(code)
        tag  = "★★" if code in both else ("외" if code in only_f else "기")
        print(f"[{i+1:02d}] {tag} {name}({code}) — 차트신호 계산 중...", end=" ", flush=True)

        sig = calculate_chart_signals(code)
        if not sig:
            print("❌ 신호 실패")
            continue

        buy_count = sig["buy_count"]
        print(f"{buy_count}/16신호", end="  →  Ollama 판단 중...", flush=True)

        decision   = _ollama_buy_decision(code, name, sig)
        action     = decision["action"]
        trade_type = decision.get("trade_type", _classify_trade_type(sig))
        reason     = decision.get("reason", "")

        emoji = "🟢 BUY" if action == "BUY" else "⛔ SKIP"
        print(f"{emoji} [{trade_type}]")

        s = sig.get("signals", {})
        def v(k): return "✅" if s.get(k) else "❌"
        print(f"       월봉 [{v('월봉_일목균형표')} {v('월봉_ADX')} {v('월봉_RSI')} {v('월봉_MACD')}]  "
              f"주봉 [{v('주봉_일목균형표')} {v('주봉_ADX')} {v('주봉_RSI')} {v('주봉_MACD')}]")
        print(f"       일봉 [{v('일봉_일목균형표')} {v('일봉_ADX')} {v('일봉_RSI')} {v('일봉_MACD')}]  "
              f"분봉 [{v('분봉_일목균형표')} {v('분봉_ADX')} {v('분봉_RSI')} {v('분봉_MACD')}]")
        print(f"       RSI={sig['rsi']} | MACD={sig['macd_hist']} | ADX={sig.get('adx',0)}")
        print(f"       이유: {reason}\n")

        results.append({
            "code": code, "name": name, "tag": tag,
            "buy_count": buy_count, "action": action,
            "trade_type": trade_type, "reason": reason,
            "rsi": sig["rsi"], "macd": sig["macd_hist"],
        })

    # ── 요약 ──────────────────────────────────────────────────────────
    print(f"\n{'='*65}")
    print("  📊 시뮬레이션 결과 요약")
    print(f"{'='*65}")
    buy_list  = [r for r in results if r["action"] == "BUY"]
    skip_list = [r for r in results if r["action"] == "SKIP"]

    print(f"\n🟢 매수 추천 {len(buy_list)}종목:")
    for r in buy_list:
        print(f"   {r['tag']} {r['name']}({r['code']})  "
              f"신호={r['buy_count']}/16  [{r['trade_type']}]  RSI={r['rsi']}")
        print(f"      └ {r['reason']}")

    print(f"\n⛔ SKIP {len(skip_list)}종목:")
    for r in skip_list:
        print(f"   {r['tag']} {r['name']}({r['code']})  신호={r['buy_count']}/16")

    print(f"\n{'='*65}")
    print(f"총 분석 {len(results)}종목 | BUY {len(buy_list)} | SKIP {len(skip_list)}")
    print(f"{'='*65}\n")

if __name__ == "__main__":
    run()
