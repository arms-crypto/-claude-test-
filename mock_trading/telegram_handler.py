#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
/mock 텔레그램 명령어 파서 및 실행기

지원 명령어:
  /mock 현황                            → 포트폴리오 현황 + 수익률
  /mock 거래내역                        → 최근 거래 10건
  /mock 삼성전자 100만원 매수           → 종목명 + 금액 매수
  /mock 매수 005930 100만원             → 코드 + 금액 매수
  /mock 매도 005930                     → 전량 매도
  /mock 매도 삼성전자 50               → 50주 매도
  /mock 충전 500만원                    → 가상 자본 충전
  /mock 출금 500만원                    → 가상 자본 출금
  /mock 백테스트 005930 20240101 20241231
"""

import re
import logging

logger = logging.getLogger(__name__)

# MockTrading 싱글턴
_mt_instance = None


def _get_mt():
    global _mt_instance
    if _mt_instance is None:
        from .mock_trading import MockTrading
        _mt_instance = MockTrading()
    return _mt_instance


def _parse_amount(text: str) -> int:
    """
    '100만원' → 1_000_000
    '1.5억'   → 150_000_000
    '500만'   → 5_000_000
    '50000'   → 50_000
    """
    text = text.replace(",", "").replace(" ", "").replace("원", "")
    m = re.search(r"(\d+(?:\.\d+)?)\s*억", text)
    if m:
        return int(float(m.group(1)) * 1_0000_0000)
    m = re.search(r"(\d+(?:\.\d+)?)\s*만", text)
    if m:
        return int(float(m.group(1)) * 10_000)
    m = re.search(r"(\d+)", text)
    if m:
        return int(m.group(1))
    return 0


HELP_TEXT = (
    "📌 *모의투자 명령어 안내*\n\n"
    "`/mock 현황` — 포트폴리오 + 수익률\n"
    "`/mock 거래내역` — 최근 거래 10건\n"
    "`/mock 삼성전자 100만원 매수`\n"
    "`/mock 매수 005930 100만원`\n"
    "`/mock 매도 005930` — 전량 매도\n"
    "`/mock 매도 005930 50` — 50주 매도\n"
    "`/mock 충전 500만원`\n"
    "`/mock 출금 200만원`\n"
    "`/mock 백테스트 005930 20240101 20241231`"
)


def parse_mock_command(text: str, oracle_pool=None) -> str:
    """
    /mock 명령어를 파싱해 MockTrading 메서드를 호출하고 결과 문자열을 반환.
    oracle_pool: proxy_v53의 DB 풀 (거래내역 Oracle 백업용, 없으면 SQLite만)
    """
    mt = _get_mt()
    t = text.strip()

    # ── 현황 ─────────────────────────────────────────────────────────────────
    if re.search(r"/mock\s*(현황|포트폴리오|상태|portfolio|status)$", t, re.I):
        return mt.get_status()

    # ── 거래내역 ─────────────────────────────────────────────────────────────
    if re.search(r"/mock\s*(거래|내역|히스토리|history)", t, re.I):
        return mt.get_history()

    # ── 충전 ─────────────────────────────────────────────────────────────────
    m = re.search(r"/mock\s+충전\s+(\S+)", t)
    if m:
        amount = _parse_amount(m.group(1))
        if amount < 1:
            return "❌ 충전 금액을 입력하세요. (예: `/mock 충전 500만원`)"
        return mt.deposit(amount)

    # ── 출금 ─────────────────────────────────────────────────────────────────
    m = re.search(r"/mock\s+출금\s+(\S+)", t)
    if m:
        amount = _parse_amount(m.group(1))
        if amount < 1:
            return "❌ 출금 금액을 입력하세요. (예: `/mock 출금 200만원`)"
        return mt.withdraw(amount)

    # ── 백테스트: /mock 백테스트 005930 20240101 20241231 ───────────────────
    m = re.search(r"/mock\s+백테스트\s+(\S+)\s+(\d{8})\s+(\d{8})", t)
    if m:
        ticker = m.group(1)
        start  = f"{m.group(2)[:4]}-{m.group(2)[4:6]}-{m.group(2)[6:]}"
        end    = f"{m.group(3)[:4]}-{m.group(3)[4:6]}-{m.group(3)[6:]}"
        return mt.backtest(ticker, start, end)

    # ── 매수: /mock 매수 삼성전자 100만원  or  /mock 삼성전자 100만원 매수 ──
    m = re.search(r"/mock\s+매수\s+(\S+)\s+(\S+)", t)
    if m:
        amount = _parse_amount(m.group(2))
        if amount < 1:
            return "❌ 금액을 확인하세요. (예: `/mock 매수 005930 100만원`)"
        return mt.buy(m.group(1), amount, oracle_pool)

    m = re.search(r"/mock\s+(.+?)\s+(\S+)\s+매수$", t)
    if m:
        amount = _parse_amount(m.group(2))
        if amount < 1:
            return "❌ 금액을 확인하세요. (예: `/mock 삼성전자 100만원 매수`)"
        return mt.buy(m.group(1), amount, oracle_pool)

    # ── 매도: /mock 매도 005930 [수량]  or  /mock 005930 매도 [수량] ─────────
    m = re.search(r"/mock\s+매도\s+(\S+)(?:\s+(\d+))?$", t)
    if m:
        qty = int(m.group(2)) if m.group(2) else None
        return mt.sell(m.group(1), qty, oracle_pool)

    m = re.search(r"/mock\s+(\S+)\s+매도(?:\s+(\d+))?$", t)
    if m:
        qty = int(m.group(2)) if m.group(2) else None
        return mt.sell(m.group(1), qty, oracle_pool)

    # ── 도움말 ───────────────────────────────────────────────────────────────
    return HELP_TEXT
