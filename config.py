#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
config.py — 설정 + 전역 상태
모든 상수, 캐시, 전역 상태 변수, 로거 정의
"""

import logging
import threading

from dotenv import load_dotenv
import os
load_dotenv()

# -------------------------
# 로깅 설정
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("proxy_v53")

# -------------------------
# 민감정보 (.env 파일에서 로드)
TOKEN_RAW = os.environ.get("TOKEN_RAW", "")
TOKEN_SRV = os.environ.get("TOKEN_SRV", "")
CHAT_ID = os.environ.get("CHAT_ID", "")
LOCAL_OLLAMA_URL = "http://localhost:11434/api/chat"
LOCAL_MODEL = "qwen2.5:7b"   # 폴백용 (tool calling 불안정 → use_tools=False로 운영)
NAVER_ID = os.environ.get("NAVER_ID", "")
NAVER_SECRET = os.environ.get("NAVER_SECRET", "")
APP_KEY = os.environ.get("APP_KEY", "")
APP_SECRET = os.environ.get("APP_SECRET", "")
DB_USER = os.environ.get("DB_USER", "admin")
DB_PASS = os.environ.get("DB_PASS", "")
DB_DSN = "nzdrpgcmwjtme3py_high"
DB_WALLET_DIR = "/home/ubuntu/oracle_task/wallet_dbname"
DB_WALLET_PASS = os.environ.get("DB_WALLET_PASS", "")
LM_API_KEY = os.environ.get("LM_API_KEY", "")
URL = "https://openapivts.koreainvestment.com:443"

# -------------------------
# 실시간 검색 설정 (SearXNG + Perplexica)
SEARXNG_URL = "http://localhost:8080"       # Docker 포트 매핑
PERPLEXICA_URL = "http://localhost:3001"    # Perplexica Backend API

# -------------------------
# 원격 Ollama 설정
REMOTE_OLLAMA_IP  = "221.144.111.116"
QWEN_URL          = f"http://{REMOTE_OLLAMA_IP}:8000/v1/chat/completions"
QWEN_MODEL        = "google_gemma-4-26b-a4b-it"   # PC Ollama 실제 모델명
LLM_MAX_RETRY     = 3

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
_auto_enabled = True           # 서비스 시작 시 자동매매 ON (재시작 시 자동 활성화)
_daily_trade_log: list    = []  # 당일 매매 내역 누적 (가상계좌) → 18:00 보고서
_daily_trade_log_ky: list = []  # 당일 매매 내역 누적 (KY 실전계좌) → 18:00 보고서
_auto_lock    = threading.Lock()
_auto_last_trades:    dict = {}  # code → {action, date, signals, rsi}  (트레이너 계좌)
_auto_last_trades_ky: dict = {}  # code → {action, date, signals, rsi}  (KY 실전계좌)
_pending_buys:     dict = {}   # code → {name, signals, time} — BUY 결정 후 신호 변화 감시용
_auto_mt_inst = None           # MockTrading 싱글턴
