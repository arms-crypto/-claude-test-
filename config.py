#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
config.py — 설정 + 전역 상태
모든 상수, 캐시, 전역 상태 변수, 로거 정의
"""

import logging
import threading

# -------------------------
# 로깅 설정
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("proxy_v53")

# -------------------------
# 민감정보
TOKEN_RAW = "8707168013:AAH5yIsoaLoxcA0Lthiw7RaIzD1YcJx8cc8"
TOKEN_SRV = "8657060115:AAEDA3L5OKmEjqdDxj3sopQlF-4BotKsvbA"  # oracleN_Agent_bot
CHAT_ID = "8448138406"
LOCAL_OLLAMA_URL = "http://localhost:11434/api/chat"
LOCAL_MODEL = "llama3.2:3b"  # tool calling 지원 (gemma2:2b → 미지원으로 변경)
NAVER_ID = "6MSVizApP3DYXeUhor5J"
NAVER_SECRET = "WCddJHD62B"
APP_KEY = "PSY9gMy15uipajb9qM25Cj1Uhf74FVu1cDyF"
APP_SECRET = "A/vwnErWUmOrZFUoJQ5bBS78WdY1lS6T6GaD5Hx1dNE+J3TTxTi1QwBvdFZuoKHWJ2nKEz+SaAmZmNikWH04Ge4Mm7up+/5JeAphHOXYld5nIbtehEmHMFcHVeB3EbNQem1pi2+0cVdyj6w7UzGJA+HqVRNFlPapifykRfPmf4Qf0IaIJdU="
DB_USER = "admin"
DB_PASS = "Flavor121212"
DB_DSN = "nzdrpgcmwjtme3py_high"
DB_WALLET_DIR = "/home/ubuntu/oracle_task/wallet_dbname"
DB_WALLET_PASS = "Flavor121212"
URL = "https://openapivts.koreainvestment.com:443"

# -------------------------
# 실시간 검색 설정 (SearXNG + Perplexica)
SEARXNG_URL = "http://localhost:8080"       # Docker 포트 매핑
PERPLEXICA_URL = "http://localhost:3001"    # Perplexica Backend API

# -------------------------
# 원격 Ollama 설정
REMOTE_OLLAMA_IP  = "221.144.111.116"
QWEN_URL          = f"http://{REMOTE_OLLAMA_IP}:11434/api/chat"
QWEN_MODEL        = "mistral-small3.1:24b"   # PC Ollama 실제 모델명
MISTRAL_MAX_RETRY = 3

# -------------------------
# Wake on LAN 설정
WOL_MAC  = "3C:7C:3F:F2:B0:41"
WOL_IP   = REMOTE_OLLAMA_IP
WOL_PORT = 9
WOL_SENT = False

# -------------------------
# 전역 캐시 및 스토어
hantu_token_cache = {"token": None, "expires_at": 0}
store = {}  # session_id -> ChatMessageHistory (deque)
verified_facts_store = {}  # session_id -> list of verified facts
pool = None  # 오라클 DB 풀

# -------------------------
# 자동매매 전역 상태
_auto_enabled = False          # /mock 자동매매 시작|종료 로 제어
_daily_trade_log: list = []    # 당일 매매 내역 누적 → 장마감 보고서에 포함
_auto_lock    = threading.Lock()
_auto_last_trades: dict = {}   # code → {action, date, signals, rsi}
_auto_mt_inst = None           # MockTrading 싱글턴
