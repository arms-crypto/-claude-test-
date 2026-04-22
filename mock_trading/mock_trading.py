#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
모의투자 엔진 (MockTrading)
- SQLite(portfolio.db): 포트폴리오/거래내역/계좌잔고 로컬 저장
- Oracle DB: 거래내역 백업 (오라클 풀은 proxy_v53에서 주입)
- KIS/Yahoo/Naver: 실시간 주가
"""

import os
import sqlite3
import logging
from datetime import datetime

import pytz
import yfinance as yf

from . import kis_client as _default_kis

logger = logging.getLogger(__name__)
KST = pytz.timezone("Asia/Seoul")

DB_PATH = os.path.join(os.path.dirname(__file__), "portfolio.db")


class MockTrading:
    INITIAL_CASH = 100_000_000  # 1억

    def __init__(self, db_path: str = DB_PATH, kis_module=None):
        self.db_path = db_path
        self._kis = kis_module or _default_kis
        self._init_db()

    # ── DB 헬퍼 ─────────────────────────────────────────────────────────────

    def _conn(self):
        return sqlite3.connect(self.db_path)

    def _init_db(self):
        with self._conn() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS portfolio (
                ticker    TEXT PRIMARY KEY,
                name      TEXT,
                qty       INTEGER,
                avg_price REAL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS trades (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker     TEXT,
                name       TEXT,
                action     TEXT,   -- BUY | SELL
                price      REAL,
                qty        INTEGER,
                amount     REAL,
                cash_after REAL,
                created_at TEXT,
                buy_signals INTEGER,  -- RAG: 매수 시 양성 신호 수 /8
                rsi         REAL,     -- RAG: 매수 시 RSI
                macd_hist   REAL,     -- RAG: 매수 시 MACD 히스트
                pnl         REAL      -- RAG: 매도 시 실현 손익률(%)
            )""")
            # 기존 DB에 컬럼이 없으면 추가 (마이그레이션)
            for col, typedef in [
                ("buy_signals", "INTEGER"),
                ("rsi",         "REAL"),
                ("macd_hist",   "REAL"),
                ("pnl",         "REAL"),
            ]:
                try:
                    db.execute(f"ALTER TABLE trades ADD COLUMN {col} {typedef}")
                except Exception:
                    pass  # 이미 존재하면 무시
            db.execute("""CREATE TABLE IF NOT EXISTS account (
                key   TEXT PRIMARY KEY,
                value TEXT
            )""")
            db.execute("INSERT OR IGNORE INTO account VALUES ('cash', ?)", [str(self.INITIAL_CASH)])
            db.commit()

    # ── 계좌 잔고 ────────────────────────────────────────────────────────────

    @property
    def cash(self) -> int:
        with self._conn() as db:
            row = db.execute("SELECT value FROM account WHERE key='cash'").fetchone()
            return int(float(row[0])) if row else self.INITIAL_CASH

    @cash.setter
    def cash(self, value: int):
        with self._conn() as db:
            db.execute("INSERT OR REPLACE INTO account VALUES ('cash', ?)", [str(value)])
            db.commit()

    def deposit(self, amount: int) -> str:
        self.cash = self.cash + amount
        return f"✅ 충전 완료: +{amount:,}원\n💰 잔고: {self.cash:,}원"

    def withdraw(self, amount: int) -> str:
        if self.cash < amount:
            return f"❌ 잔고 부족 (현재: {self.cash:,}원)"
        self.cash = self.cash - amount
        return f"✅ 출금 완료: -{amount:,}원\n💰 잔고: {self.cash:,}원"

    # ── 내부 유틸 ────────────────────────────────────────────────────────────

    def _get_holdings(self) -> list:
        with self._conn() as db:
            return db.execute(
                "SELECT ticker, name, qty, avg_price FROM portfolio WHERE qty > 0"
            ).fetchall()

    def _record_trade(self, ticker, name, action, price, qty, cash_after, oracle_pool=None,
                      buy_signals=None, rsi=None, macd_hist=None, pnl=None):
        now_str = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
        amount = price * qty
        with self._conn() as db:
            db.execute(
                """INSERT INTO trades
                   (ticker, name, action, price, qty, amount, cash_after, created_at,
                    buy_signals, rsi, macd_hist, pnl)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [ticker, name, action, price, qty, amount, cash_after, now_str,
                 buy_signals, rsi, macd_hist, pnl],
            )
            db.commit()
        # Oracle 백업 (풀이 주입된 경우)
        if oracle_pool:
            self._save_oracle(oracle_pool, ticker, name, action, price, qty, amount, cash_after)

    def _save_oracle(self, pool, ticker, name, action, price, qty, amount, cash_after):
        try:
            with pool.acquire() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """INSERT INTO mock_trades
                           (ticker, name, action, price, qty, amount, cash_after, created_at)
                           VALUES (:1,:2,:3,:4,:5,:6,:7,CURRENT_TIMESTAMP)""",
                        [ticker, name, action, price, qty, amount, cash_after],
                    )
                    conn.commit()
        except Exception:
            logger.exception("Oracle mock_trades 저장 실패 (무시)")

    # ── 매수 ─────────────────────────────────────────────────────────────────

    def buy(self, name_or_code: str, amount_krw: int, oracle_pool=None,
            buy_signals=None, rsi=None, macd_hist=None, limit_price: int = 0) -> str:
        code, name = self._kis.resolve_code(name_or_code)
        if not code:
            return f"❌ '{name_or_code}' 종목을 찾을 수 없습니다."

        price = limit_price if limit_price > 0 else self._kis.get_price(code)
        if not price:
            return f"❌ {name}({code}) 가격 조회 실패"

        qty = int(amount_krw / price)
        if qty < 1:
            return f"❌ 수량 부족 (1주 {price:,}원, 요청 {amount_krw:,}원)"

        # 주문 직전 실잔고 최종 확인 (get_available_amount 후 잔고 변동 대비)
        try:
            order_price = limit_price if limit_price > 0 else price
            avail = self._kis.get_available_amount(code, order_price)
            cost_needed = qty * order_price
            if 0 < avail < cost_needed:
                qty = int(avail / order_price)
                if qty < 1:
                    return f"❌ {name}({code}) 주문가능금액 부족 ({avail:,}원 < {order_price:,}원/주)"
        except Exception:
            pass

        # KIS 매수 주문 (limit_price>0 이면 지정가)
        result = self._kis.buy_stock(code, qty, price=limit_price)
        if not result["success"]:
            msg = result["msg"]
            if "주문가능금액" in msg or "금액을 초과" in msg:
                return f"❌ NXT 매수 불가 (가용금액 부족): {msg}"
            return f"❌ KIS 매수 실패: {msg}"

        cost = qty * price
        # 로컬 DB에도 기록 (히스토리용)
        with self._conn() as db:
            row = db.execute(
                "SELECT qty, avg_price FROM portfolio WHERE ticker=?", [code]
            ).fetchone()
            if row:
                new_qty = row[0] + qty
                new_avg = (row[0] * row[1] + cost) / new_qty
                db.execute(
                    "UPDATE portfolio SET qty=?, avg_price=?, name=? WHERE ticker=?",
                    [new_qty, new_avg, name, code],
                )
            else:
                db.execute(
                    "INSERT INTO portfolio VALUES (?, ?, ?, ?)",
                    [code, name, qty, price],
                )
            db.commit()

        self.cash = self.cash - cost
        self._record_trade(code, name, "BUY", price, qty, self.cash, oracle_pool,
                           buy_signals=buy_signals, rsi=rsi, macd_hist=macd_hist)

        return (
            f"✅ KIS 모의 매수 완료!\n"
            f"📈 {name}({code}): {qty:,}주 × {price:,}원 = {cost:,}원\n"
            f"🔖 주문번호: {result['order_no']}\n"
            f"💰 잔고: {self.cash:,}원"
        )

    # ── 매도 ─────────────────────────────────────────────────────────────────

    def sell(self, name_or_code: str, qty: int = None, oracle_pool=None) -> str:
        code, name = self._kis.resolve_code(name_or_code)
        if not code:
            return f"❌ '{name_or_code}' 종목을 찾을 수 없습니다."

        with self._conn() as db:
            row = db.execute(
                "SELECT qty, avg_price, name FROM portfolio WHERE ticker=?", [code]
            ).fetchone()
        if not row or row[0] == 0:
            return f"❌ {name}({code}) 보유 없음"

        hold_qty, avg_price, db_name = row
        name = db_name or name
        sell_qty = qty if qty else hold_qty
        if sell_qty > hold_qty:
            return f"❌ 보유 수량 부족 (보유 {hold_qty:,}주)"

        # KIS 매도가능수량(ord_psbl_qty)으로 캡핑 — T+2 미결제·불일치 초과주문 방지
        try:
            bal = self._kis.get_balance()
            holding = next((h for h in bal.get("holdings", []) if h["code"] == code), None)
            if holding is not None:
                # sell_qty = 주문가능수량 (T+2 미결제 제외), qty = 총보유수량
                kis_sellable = holding.get("sell_qty", holding["qty"])
                if sell_qty > kis_sellable:
                    logger.warning(
                        "sell_qty 조정: %d → KIS매도가능%d (%s)", sell_qty, kis_sellable, code)
                    sell_qty = kis_sellable
            if sell_qty <= 0:
                return f"❌ {name}({code}) KIS 매도가능수량 0주"
        except Exception:
            logger.warning("get_balance 실패 — 매도가능수량 캡핑 불가: %s", code)

        price = self._kis.get_price(code)
        if not price:
            return f"❌ {name}({code}) 가격 조회 실패"

        # KIS 매도 주문
        result = self._kis.sell_stock(code, sell_qty)
        if not result["success"] and "수량을 초과" in result.get("msg", ""):
            # 주문가능수량 초과 — 잔고 재조회 후 1회 재시도 (NXT T+2 미반영 대응)
            try:
                bal2 = self._kis.get_balance()
                h2 = next((h for h in bal2.get("holdings", []) if h["code"] == code), None)
                retry_qty = h2.get("sell_qty", 0) if h2 else 0
                if retry_qty > 0 and retry_qty < sell_qty:
                    logger.warning("매도수량 재조회 재시도: %d→%d (%s)", sell_qty, retry_qty, code)
                    result = self._kis.sell_stock(code, retry_qty)
                    if result["success"]:
                        sell_qty = retry_qty
                elif retry_qty == 0:
                    return f"❌ {name}({code}) KIS 재조회 매도가능수량 0주"
            except Exception:
                pass
        if not result["success"]:
            msg = result["msg"]
            if "수량을 초과" in msg:
                return f"❌ NXT 매도 불가 (T+2 미결제): {msg}"
            return f"❌ KIS 실전 매도 실패: {msg}"

        proceeds = sell_qty * price
        profit = (price - avg_price) * sell_qty
        pct = (price - avg_price) / avg_price * 100

        with self._conn() as db:
            new_qty = hold_qty - sell_qty
            if new_qty == 0:
                db.execute("DELETE FROM portfolio WHERE ticker=?", [code])
            else:
                db.execute("UPDATE portfolio SET qty=? WHERE ticker=?", [new_qty, code])
            db.commit()

        self.cash = self.cash + proceeds
        self._record_trade(code, name, "SELL", price, sell_qty, self.cash, oracle_pool,
                           pnl=round(pct, 2))

        sign = "+" if profit >= 0 else ""
        return (
            f"✅ KIS 모의 매도 완료!\n"
            f"📉 {name}({code}): {sell_qty:,}주 × {price:,}원 = {proceeds:,}원\n"
            f"{'📈' if profit >= 0 else '📉'} 손익: {sign}{int(profit):,}원 ({sign}{pct:.1f}%)\n"
            f"🔖 주문번호: {result['order_no']}\n"
            f"💰 잔고: {self.cash:,}원"
        )

    # ── 현황 ─────────────────────────────────────────────────────────────────

    def get_status(self) -> str:
        # KIS 잔고 실시간 조회
        bal = self._kis.get_balance()
        cash = bal["cash"] or self.cash
        kis_holdings = bal["holdings"]

        lines = ["📊 *KIS 모의투자 현황*\n", f"💵 현금: {cash:,}원\n"]

        if not kis_holdings:
            lines.append("📭 보유 종목 없음")
            return "\n".join(lines)

        total_invest = 0
        total_eval = 0
        lines.append("─────────────────")

        for h in kis_holdings:
            code      = h["code"]
            name      = h["name"]
            qty       = h["qty"]
            avg_price = h["avg_price"]
            price     = h["current_price"] or self._kis.get_price(code) or avg_price
            invest    = avg_price * qty
            eval_val  = price * qty
            profit    = eval_val - invest
            pct       = h["pnl"]
            total_invest += invest
            total_eval   += eval_val
            sign = "▲" if profit >= 0 else "▼"
            lines.append(f"{sign} {name}({code})")
            lines.append(f"   {qty:,}주 | 평단 {int(avg_price):,}원 → 현재 {price:,}원")
            p_str = f"+{int(profit):,}" if profit >= 0 else f"{int(profit):,}"
            lines.append(f"   평가: {int(eval_val):,}원 | 손익: {p_str}원 ({'+' if pct>=0 else ''}{pct:.1f}%)")

        lines.append("─────────────────")
        total_profit = total_eval - total_invest
        total_pct = total_profit / total_invest * 100 if total_invest else 0
        total_assets = cash + total_eval
        p_str = f"+{int(total_profit):,}" if total_profit >= 0 else f"{int(total_profit):,}"
        lines.append(f"📈 총 평가금액: {int(total_eval):,}원")
        lines.append(
            f"{'📈' if total_profit >= 0 else '📉'} 총 손익: {p_str}원 "
            f"({'+' if total_pct>=0 else ''}{total_pct:.1f}%)"
        )
        lines.append(f"💼 총 자산: {int(total_assets):,}원")
        return "\n".join(lines)

    # ── 거래내역 ─────────────────────────────────────────────────────────────

    def get_history(self, limit: int = 10) -> str:
        with self._conn() as db:
            rows = db.execute(
                """SELECT action, name, ticker, price, qty, amount, created_at
                   FROM trades ORDER BY id DESC LIMIT ?""",
                [limit],
            ).fetchall()
        if not rows:
            return "거래 내역 없음"
        lines = [f"📋 *최근 거래 내역* (최근 {limit}건)\n"]
        for action, name, ticker, price, qty, amount, ts in rows:
            icon = "🟢" if action == "BUY" else "🔴"
            lines.append(
                f"{icon} {action} | {name}({ticker}) | {qty:,}주 | "
                f"{int(price):,}원 | {int(amount):,}원 | {ts}"
            )
        return "\n".join(lines)

    # ── 백테스트 ─────────────────────────────────────────────────────────────

    def backtest(self, name_or_code: str, start: str, end: str) -> str:
        """
        start / end: 'YYYY-MM-DD' 형식
        가정: 시작일에 1,000만원 전액 매수 → 종료일 종가에 매도
        """
        code, name = resolve_code(name_or_code)
        if not code:
            return f"❌ '{name_or_code}' 종목을 찾을 수 없습니다."
        try:
            df = yf.download(f"{code}.KS", start=start, end=end, progress=False, auto_adjust=True)
            if df.empty:
                return f"❌ {name}({code}) 기간 데이터 없음 ({start}~{end})"

            start_price = float(df["Close"].iloc[0])
            end_price   = float(df["Close"].iloc[-1])
            invest      = 10_000_000
            qty_bt      = int(invest / start_price)
            profit      = (end_price - start_price) * qty_bt
            profit_pct  = (end_price - start_price) / start_price * 100
            min_p       = float(df["Close"].min())
            max_p       = float(df["Close"].max())
            days        = len(df)
            sign        = "+" if profit >= 0 else ""

            return (
                f"📊 *백테스트: {name}({code})*\n"
                f"기간: {start} ~ {end} ({days}거래일)\n"
                f"시작가: {int(start_price):,}원\n"
                f"종료가: {int(end_price):,}원\n"
                f"최저: {int(min_p):,}원 | 최고: {int(max_p):,}원\n"
                f"수익률: {sign}{profit_pct:.1f}%\n"
                f"수익금: {sign}{int(profit):,}원 (1,000만원 투자 기준)"
            )
        except Exception:
            logger.exception("backtest 실패: %s", name_or_code)
            return "❌ 백테스트 오류 (로그 확인)"
