#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
performance_tracker.py — 실거래 성과 추적
일별/누적 수익률, 승률, MDD, 업종별 성과 분석

사용법:
  python3 performance_tracker.py          # 전체 보고서
  python3 performance_tracker.py --short  # 한 줄 요약 (evening_report용)
  python3 performance_tracker.py --json   # JSON 출력
"""

import sqlite3
import json
import sys
from collections import defaultdict
from pathlib import Path

INITIAL_BALANCE = 100_000_000  # 초기 잔고 1억 (2026-04-07 초기화)

DB_ACCOUNTS = [
    ("🔵 트레이너", Path(__file__).parent / "mock_trading/portfolio.db"),
    ("🟡 KY",       Path(__file__).parent / "mock_trading/portfolio_ky.db"),
]

# 업종 매핑 (auto_trader.py _CODE_TO_SECTOR 기준)
_SECTOR = {
    "005930": "반도체", "000660": "반도체", "009150": "반도체",
    "005380": "자동차", "012330": "자동차", "000270": "자동차",
    "068270": "바이오", "207940": "바이오", "091990": "바이오",
    "035720": "IT플랫폼", "035420": "IT플랫폼", "259960": "IT플랫폼",
    "105560": "금융", "055550": "금융", "086790": "금융",
    "010950": "에너지", "010060": "에너지", "096770": "에너지",
    "082740": "방산", "012450": "방산", "047810": "방산",
    "267270": "기계", "042660": "기계", "329180": "기계",
}


EFFECTIVE_START = "2026-04-24"  # 정상 가동 기준일 (이전은 코드 수정 과도기)


def analyze(db_path: Path, label: str, start_date: str = EFFECTIVE_START) -> dict:
    """단일 계좌 성과 전체 분석.

    Args:
        start_date: 분석 시작일 (기본: EFFECTIVE_START). None이면 전체 데이터.
    """
    with sqlite3.connect(db_path) as con:
        if start_date:
            sells = con.execute("""
                SELECT DATE(created_at) dt, name, ticker, pnl, amount, cash_after
                FROM trades WHERE action='SELL' AND pnl IS NOT NULL
                  AND DATE(created_at) >= ?
                ORDER BY id
            """, [start_date]).fetchall()
        else:
            sells = con.execute("""
                SELECT DATE(created_at) dt, name, ticker, pnl, amount, cash_after
                FROM trades WHERE action='SELL' AND pnl IS NOT NULL
                ORDER BY id
            """).fetchall()

        bal = con.execute("SELECT value FROM account WHERE key='cash'").fetchone()
        cash = float(bal[0]) if bal else 0

        holdings = con.execute(
            "SELECT name, ticker, qty, avg_price FROM portfolio WHERE qty > 0"
        ).fetchall()

    holding_value = sum(qty * avg for _, _, qty, avg in holdings)
    total_value   = cash + holding_value  # 장부가 기준

    all_pnls = [r[3] for r in sells]
    wins     = [p for p in all_pnls if p > 0]
    losses   = [p for p in all_pnls if p <= 0]
    win_rate = len(wins) / len(all_pnls) * 100 if all_pnls else 0
    avg_win  = sum(wins)   / len(wins)   if wins   else 0
    avg_loss = sum(losses) / len(losses) if losses else 0
    profit_factor = abs(sum(wins) / sum(losses)) if losses and sum(losses) != 0 else float("inf")

    # 실현 손익: 각 매도건의 pnl% × 매도금액 합산
    realized_pnl = sum(r[3] / 100 * r[4] for r in sells if r[4])  # 원화 기준

    # 일별 평균 손익 기준 최대 손실일 (MDD 대용 — 데이터 적을 때 신뢰성 있음)
    mdd = 0.0  # 최악 일일 평균 손익(%)
    daily_avgs = []
    tmp = defaultdict(list)
    for dt, name, ticker, pnl, amount, _ in sells:
        tmp[dt].append(pnl)
    for dt, pnls in tmp.items():
        avg = sum(pnls) / len(pnls)
        daily_avgs.append(avg)
    if daily_avgs:
        mdd = min(daily_avgs)  # 음수 = 손실, 0 이상이면 손실 없음

    # 일별 통계
    daily = defaultdict(lambda: {"sells": 0, "wins": 0, "pnl_list": []})
    for dt, name, ticker, pnl, amount, _ in sells:
        daily[dt]["sells"] += 1
        if pnl > 0:
            daily[dt]["wins"] += 1
        daily[dt]["pnl_list"].append(pnl)

    # 업종별 통계
    sector_stat = defaultdict(lambda: {"sells": 0, "wins": 0, "pnl_list": []})
    for dt, name, ticker, pnl, amount, _ in sells:
        sec = _SECTOR.get(ticker, "기타")
        sector_stat[sec]["sells"] += 1
        if pnl > 0:
            sector_stat[sec]["wins"] += 1
        sector_stat[sec]["pnl_list"].append(pnl)

    return {
        "label":         label,
        "total_value":   total_value,
        "cash":          cash,
        "holding_value": holding_value,
        "realized_pnl":  realized_pnl,
        "total_sells":   len(all_pnls),
        "win_count":     len(wins),
        "loss_count":    len(losses),
        "win_rate":      win_rate,
        "avg_win":       avg_win,
        "avg_loss":      avg_loss,
        "profit_factor": profit_factor,
        "mdd":           mdd,
        "daily":         {k: v for k, v in sorted(daily.items())},
        "sector":        dict(sector_stat),
        "holdings":      holdings,
    }


def format_full(r: dict) -> str:
    pnl_sign = "+" if r["realized_pnl"] >= 0 else ""
    pf = f"{r['profit_factor']:.2f}" if r["profit_factor"] != float("inf") else "∞"
    lines = [
        f"{'─'*40}",
        f"📊 {r['label']} 성과 분석",
        f"{'─'*40}",
        f"총 평가금액(장부가): {r['total_value']:>12,.0f}원",
        f"  예수금:            {r['cash']:>12,.0f}원",
        f"  보유평가(장부가):  {r['holding_value']:>12,.0f}원",
        f"실현 손익:          {pnl_sign}{r['realized_pnl']:>12,.0f}원",
        f"",
        f"📈 매매 통계 (총 {r['total_sells']}건)",
        f"  승률: {r['win_rate']:.1f}%  ({r['win_count']}승 {r['loss_count']}패)",
        f"  평균 수익: {r['avg_win']:+.2f}%  |  평균 손실: {r['avg_loss']:.2f}%",
        f"  Profit Factor: {pf}",
        f"  최악 일평균: {r['mdd']:+.2f}%  ({min(r['daily'].keys(), default='-')} 기준)",
        f"",
        f"📅 일별 성과",
    ]
    for dt, d in r["daily"].items():
        wr  = d["wins"] / d["sells"] * 100 if d["sells"] else 0
        avg = sum(d["pnl_list"]) / d["sells"] if d["sells"] else 0
        lines.append(f"  {dt}: {d['sells']}건 승률{wr:.0f}% 평균{avg:+.1f}%")

    if r["sector"]:
        lines.append(f"")
        lines.append(f"🏭 업종별 성과")
        for sec, d in sorted(r["sector"].items(), key=lambda x: -len(x[1]["pnl_list"])):
            wr  = d["wins"] / d["sells"] * 100 if d["sells"] else 0
            avg = sum(d["pnl_list"]) / d["sells"] if d["sells"] else 0
            lines.append(f"  {sec:8s}: {d['sells']}건 승률{wr:.0f}% 평균{avg:+.1f}%")

    if r["holdings"]:
        lines.append(f"")
        lines.append(f"💼 현재 보유 ({len(r['holdings'])}종목)")
        for name, ticker, qty, avg in r["holdings"]:
            lines.append(f"  {name}({ticker}) {qty}주 평단{int(avg):,}원")

    return "\n".join(lines)


def format_short(r: dict) -> str:
    pnl_sign = "+" if r["realized_pnl"] >= 0 else ""
    return (
        f"{r['label']}: 실현손익{pnl_sign}{r['realized_pnl']:,.0f}원 | "
        f"승률{r['win_rate']:.0f}%({r['win_count']}승{r['loss_count']}패) | "
        f"최악일평균{r['mdd']:+.1f}%"
    )


if __name__ == "__main__":
    short_mode = "--short" in sys.argv
    json_mode  = "--json"  in sys.argv
    all_mode   = "--all"   in sys.argv  # 전체 기간 (기준일 필터 없음)

    start = None if all_mode else EFFECTIVE_START

    results = []
    for label, db_path in DB_ACCOUNTS:
        if not db_path.exists():
            continue
        try:
            r = analyze(db_path, label, start_date=start)
            if r["total_sells"] == 0:
                continue  # 거래 없는 계좌 스킵
            results.append(r)
        except Exception as e:
            print(f"[{label}] 분석 실패: {e}", file=sys.stderr)

    if json_mode:
        # JSON 직렬화 (holdings tuple 처리)
        out = []
        for r in results:
            r2 = dict(r)
            r2["holdings"] = [{"name": n, "ticker": t, "qty": q, "avg": a}
                               for n, t, q, a in r["holdings"]]
            out.append(r2)
        print(json.dumps(out, ensure_ascii=False, indent=2))
    elif short_mode:
        for r in results:
            print(format_short(r))
    else:
        for r in results:
            print(format_full(r))
            print()
