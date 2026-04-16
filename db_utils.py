#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
db_utils.py — Oracle DB 유틸리티
get_db_pool(), save_fact_to_db(), get_stock_code_from_db(),
save_stock_code_to_db(), init_krx_db(), ensure_db_initialized(),
init_stock_codes_db()
"""

import requests
import pandas as pd
import oracledb
import config

logger = config.logger


def get_db_pool():
    if config.pool is None:
        try:
            logger.info("오라클 DB 연결 시도...")
            config.pool = oracledb.create_pool(
                user=config.DB_USER,
                password=config.DB_PASS,
                dsn=config.DB_DSN,
                min=2,
                max=5,
                config_dir=config.DB_WALLET_DIR,
                wallet_location=config.DB_WALLET_DIR,
                wallet_password=config.DB_WALLET_PASS
            )
            logger.info("오라클 DB 풀 생성 성공")
        except Exception:
            logger.exception("DB 풀 생성 실패")
            config.pool = None
    return config.pool


_CATEGORY_RULES = [
    ("TECH",    ["반도체", "AI", "인공지능", "엔비디아", "삼성전자", "SK하이닉스", "배터리",
                 "전기차", "테슬라", "로봇", "소프트웨어", "IT", "플랫폼", "클라우드",
                 "Reuters", "CNBC", "MarketWatch", "Yahoo Finance"]),
    ("COMPANY", ["삼성", "LG", "현대", "SK", "카카오", "네이버", "포스코", "롯데",
                 "한화", "두산", "KT", "기업", "실적", "영업이익", "적자", "흑자"]),
    ("ECONOMY", ["금리", "환율", "달러", "인플레이션", "연준", "FOMC", "GDP", "CPI",
                 "무역", "관세", "트럼프", "국채", "원자재", "유가", "금값", "경제"]),
    ("MARKET",  ["코스피", "코스닥", "나스닥", "S&P", "증시", "주가", "주식",
                 "외국인", "기관", "매수", "매도", "시황", "장마감", "장중"]),
]

def _auto_classify(text: str) -> str:
    """키워드 기반 자동 카테고리 분류."""
    for category, keywords in _CATEGORY_RULES:
        if any(k in text for k in keywords):
            return category
    return "MARKET"


def save_fact_to_db(content: str, category: str = None):
    """Oracle daily_news에 뉴스 저장. category=None이면 자동 분류."""
    p = get_db_pool()
    if not p:
        logger.warning("DB 풀 없음: save_fact_to_db 스킵")
        return
    resolved_category = category if category else _auto_classify(content)
    try:
        with p.acquire() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO daily_news (headlines, category, run_time) VALUES (:1, :2, CURRENT_TIMESTAMP)",
                    [str(content)[:1000], resolved_category]
                )
                conn.commit()
                logger.info("DB 뉴스 저장 완료 (category=%s)", resolved_category)
    except Exception:
        logger.exception("DB 저장 오류")


def get_stock_code_from_db(name: str) -> str:
    p = get_db_pool()
    if not p:
        return None
    try:
        with p.acquire() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT code FROM stock_codes
                    WHERE UPPER(TRIM(name)) LIKE UPPER(:1)
                    ORDER BY LENGTH(name)
                    FETCH FIRST 1 ROWS ONLY
                """, [f"%{name.strip()}%"])
                res = cur.fetchone()
                return res[0] if res else None
    except Exception:
        logger.exception("get_stock_code_from_db 오류")
        return None


def save_stock_code_to_db(name: str, code: str) -> bool:
    p = get_db_pool()
    if not p:
        logger.warning("DB 풀 없음: save_stock_code_to_db 스킵")
        return False
    try:
        with p.acquire() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT code FROM stock_codes WHERE name = :1", [name])
                if cur.fetchone():
                    cur.execute("UPDATE stock_codes SET code = :2, updated = CURRENT_TIMESTAMP WHERE name = :1", [name, code])
                else:
                    cur.execute("INSERT INTO stock_codes (name, code) VALUES (:1, :2)", [name, code])
                conn.commit()
                logger.info("종목코드 DB 저장: %s=%s", name, code)
                return True
    except Exception:
        logger.exception("save_stock_code_to_db 오류")
        return False


def init_krx_db():
    logger.info("KRX 종목 DB 생성 시작...")
    try:
        url = "http://kind.krx.co.kr/corpgeneral/corpList.do?method=download"
        params = {'marketType': 'stockMkt', 'searchCodeType': '', 'pageIndex': '1'}
        r = requests.post(url, params=params, timeout=15)
        r.raise_for_status()
        df = pd.read_html(r.text)[0]
        df['회사명'] = df['회사명'].str.strip().str.replace(' ', '')
        df['종목코드'] = df['종목코드'].astype(str).str.zfill(6)
        p = get_db_pool()
        if p:
            with p.acquire() as conn:
                with conn.cursor() as cur:
                    try:
                        cur.execute("DELETE FROM stock_codes")
                    except Exception:
                        pass
                    for _, row in df.iterrows():
                        cur.execute("INSERT INTO stock_codes (name, code) VALUES (:1, :2)", [row['회사명'], row['종목코드']])
                    conn.commit()
            logger.info("KRX 종목 DB 생성 완료: %d 종목", len(df))
        return dict(zip(df['종목코드'], df['회사명']))
    except Exception:
        logger.exception("init_krx_db 실패")
        return {}


def init_stock_codes_db():
    p = get_db_pool()
    if p:
        try:
            with p.acquire() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        CREATE TABLE stock_codes (
                            name VARCHAR2(50) PRIMARY KEY,
                            code VARCHAR2(10),
                            updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            fail_count NUMBER DEFAULT 0
                        )
                    """)
                    conn.commit()
                    logger.info("stock_codes 테이블 생성")
                    cur.execute("""
                        MERGE INTO stock_codes t
                        USING (SELECT 'LG엔솔' name, '373220' code FROM dual) s
                        ON (t.name = s.name)
                        WHEN MATCHED THEN UPDATE SET code = s.code
                        WHEN NOT MATCHED THEN INSERT (name, code) VALUES (s.name, s.code)
                    """)
                    conn.commit()
                    logger.info("LG엔솔(373220) DB 등록 완료")
        except Exception as e:
            if "ORA-00955" in str(e):
                logger.warning("테이블 이미 존재")
            else:
                logger.exception("init_stock_codes_db 예외")
        # mock_trades 테이블 (모의투자 거래내역 Oracle 백업)
        try:
            with p.acquire() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        CREATE TABLE mock_trades (
                            id         NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                            ticker     VARCHAR2(10),
                            name       VARCHAR2(50),
                            action     VARCHAR2(4),
                            price      NUMBER,
                            qty        NUMBER,
                            amount     NUMBER,
                            cash_after NUMBER,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        )
                    """)
                    conn.commit()
                    logger.info("mock_trades 테이블 생성")
        except Exception as e:
            if "ORA-00955" in str(e):
                logger.info("mock_trades 테이블 이미 존재")
            else:
                logger.exception("mock_trades 테이블 생성 실패 (무시)")
        # mock_smart_flows 테이블 (기관/외국인 순매수 TOP100, 6개월 보존)
        try:
            with p.acquire() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        CREATE TABLE mock_smart_flows (
                            id             NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                            collected_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            date_str       VARCHAR2(8),
                            investor_type  VARCHAR2(10),
                            rank_no        NUMBER,
                            ticker         VARCHAR2(10),
                            name           VARCHAR2(50),
                            net_buy_amount NUMBER
                        )
                    """)
                    conn.commit()
                    logger.info("mock_smart_flows 테이블 생성")
        except Exception as e:
            if "ORA-00955" in str(e):
                logger.info("mock_smart_flows 테이블 이미 존재")
            else:
                logger.exception("mock_smart_flows 테이블 생성 실패 (무시)")


def ensure_db_initialized():
    """
    앱 시작 시 한 번만 안전하게 DB 초기화를 수행합니다.
    """
    try:
        init_stock_codes_db()
    except Exception:
        logger.exception("ensure_db_initialized 예외")
