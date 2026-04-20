#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
daily_report_gemma.py — 장 마감 후 로그 분석 → Gemma 4 보고서 → 텔레그램 전송
실행: python3 daily_report_gemma.py
크론: 평일 15:40
"""

import os
import sys
import re
import datetime
import requests

# ── 설정 ──────────────────────────────────────────────────────────────────────
LOG_PATH      = os.path.join(os.path.dirname(__file__), "proxy_v54.log")
LM_URL        = "http://221.144.111.116:8000/v1/chat/completions"
LM_API_KEY    = os.environ.get("LM_API_KEY", "")
GEMMA_MODEL   = "google_gemma-4-26b-a4b-it"
TOKEN_RAW     = os.environ.get("TOKEN_RAW", "")
CHAT_ID       = os.environ.get("CHAT_ID", "")
TG_URL        = f"https://api.telegram.org/bot{TOKEN_RAW}/sendMessage"

TODAY = datetime.date.today().strftime("%Y-%m-%d")


# ── 로그 파싱 ─────────────────────────────────────────────────────────────────

def parse_log():
    """오늘 날짜 로그에서 핵심 항목 추출."""
    if not os.path.exists(LOG_PATH):
        return {}

    lines = []
    try:
        with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if TODAY in line:
                    lines.append(line.rstrip())
    except Exception as e:
        print(f"로그 읽기 실패: {e}")
        return {}

    # [DAILY_REPORT] — 장마감 최종 요약
    daily_reports = [l for l in lines if "[DAILY_REPORT]" in l]

    # [REPORT] — 스캔 결과 + 사이클 누적
    scan_reports = [l for l in lines if "[REPORT] 스캔BUY후보" in l]
    cycle_reports = [l for l in lines if "[REPORT]" in l and "누적매수" in l]

    # 실제 매수/매도 체결
    buys  = [l for l in lines if "🚀" in l and "신규매수" in l]
    sells = [l for l in lines if any(e in l for e in ["🔴", "💰", "🤑", "🟡"]) and ("전량매도" in l or "부분매도" in l)]

    # 에러
    errors = [l for l in lines if " ERROR " in l]

    # 마지막 스캔 결과만
    last_scan = scan_reports[-1] if scan_reports else ""
    last_cycle = {r.split("]")[1].split("누적")[0].strip(): r for r in cycle_reports}

    return {
        "daily_reports": daily_reports,
        "last_scan": last_scan,
        "last_cycle": list(last_cycle.values()),
        "buys": buys,
        "sells": sells,
        "errors": errors[:5],
    }


def build_summary(data: dict) -> str:
    """Gemma에 넘길 로그 요약 텍스트 구성."""
    lines = [f"📅 {TODAY} 자동매매 로그 요약\n"]

    if data.get("daily_reports"):
        lines.append("=== 장마감 최종 요약 ===")
        for r in data["daily_reports"]:
            m = re.search(r"\[DAILY_REPORT\] (.+)", r)
            if m:
                lines.append(m.group(1))

    if data.get("last_scan"):
        m = re.search(r"\[REPORT\] (.+)", data["last_scan"])
        if m:
            lines.append(f"\n=== 마지막 스캔 결과 ===\n{m.group(1)}")

    if data.get("buys"):
        lines.append(f"\n=== 매수 체결 ({len(data['buys'])}건) ===")
        for b in data["buys"][-10:]:
            m = re.search(r"신규매수 (.+?) \[(.+?)\]: ([\d,]+)원", b)
            if m:
                lines.append(f"  • {m.group(1)} [{m.group(2)}] {m.group(3)}원")

    if data.get("sells"):
        lines.append(f"\n=== 매도 체결 ({len(data['sells'])}건) ===")
        for s in data["sells"][-10:]:
            m = re.search(r"(전량매도|부분매도) (.+?)\((.+?)\): ([+\-\d.]+)%", s)
            if m:
                lines.append(f"  • {m.group(2)}({m.group(3)}) {m.group(1)} {m.group(4)}%")

    if data.get("errors"):
        lines.append(f"\n=== 에러 ({len(data['errors'])}건) ===")
        for e in data["errors"][:3]:
            m = re.search(r"ERROR (.+)", e)
            if m:
                lines.append(f"  ⚠️ {m.group(1)[:100]}")

    return "\n".join(lines)


# ── Gemma 호출 ────────────────────────────────────────────────────────────────

def call_gemma(summary: str) -> str:
    prompt = (
        "아래는 오늘 자동매매 시스템의 로그 요약이야. "
        "투자자가 하루를 마감하며 읽을 수 있도록 간결하게 보고서를 작성해줘.\n"
        "형식: 📊 오늘 매매 요약 → 매수/매도 건수, 특이사항, 에러 유무를 3~5줄로.\n"
        "수치는 그대로 사용하고 추측하지 말 것.\n\n"
        f"{summary}"
    )
    try:
        resp = requests.post(
            LM_URL,
            headers={"Authorization": f"Bearer {LM_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": GEMMA_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 512,
                "temperature": 0.3,
            },
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"Gemma 호출 실패: {e}\n\n[원본 요약]\n{summary}"


# ── 텔레그램 전송 ─────────────────────────────────────────────────────────────

def send_telegram(text: str):
    if not TOKEN_RAW or not CHAT_ID:
        print("텔레그램 토큰/CHAT_ID 없음 — 콘솔 출력만")
        print(text)
        return
    try:
        requests.post(TG_URL, json={"chat_id": CHAT_ID, "text": text}, timeout=10)
        print("텔레그램 전송 완료")
    except Exception as e:
        print(f"텔레그램 전송 실패: {e}")
        print(text)


# ── 메인 ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"[daily_report_gemma] {TODAY} 로그 분석 시작")
    data = parse_log()

    if not data.get("buys") and not data.get("sells") and not data.get("daily_reports"):
        print("오늘 매매 기록 없음 — 종료")
        sys.exit(0)

    summary = build_summary(data)
    print("--- 로그 요약 ---")
    print(summary)
    print("-----------------")

    report = call_gemma(summary)
    print("--- Gemma 보고서 ---")
    print(report)
    print("-------------------")

    send_telegram(f"📈 일일 매매 보고서 ({TODAY})\n\n{report}")
