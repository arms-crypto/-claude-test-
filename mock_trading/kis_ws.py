#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
kis_ws.py — KIS H0STCNI0 체결통보 WebSocket 리스너
체결 발생 시 on_fill(fill_dict) 콜백 호출.
"""

import json
import logging
import threading
import time
from base64 import b64decode
from datetime import datetime

import pytz
import websocket  # websocket-client

logger = logging.getLogger(__name__)
# websocket-client 라이브러리가 ERROR 레벨로 "goodbye" 등을 찍어 에러 대시보드를 오염시킴
logging.getLogger("websocket").setLevel(logging.CRITICAL)

KST = pytz.timezone("Asia/Seoul")

WS_URL = "ws://ops.koreainvestment.com:21000"

FILL_COLUMNS = [
    "CUST_ID", "ACNT_NO", "ODER_NO", "OODER_NO", "SELN_BYOV_CLS",
    "RCTF_CLS", "ODER_KIND", "ODER_COND", "STCK_SHRN_ISCD", "CNTG_QTY",
    "CNTG_UNPR", "STCK_CNTG_HOUR", "RFUS_YN", "CNTG_YN", "ACPT_YN",
    "BRNC_NO", "ODER_QTY", "ACNT_NAME", "ORD_COND_PRC", "ORD_EXG_GB",
    "POPUP_YN", "FILLER", "CRDT_CLS", "CRDT_LOAN_DATE", "CNTG_ISNM40", "ODER_PRC",
]


def _aes_dec(key: str, iv: str, cipher_text: str) -> str:
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import unpad
    cipher = AES.new(key.encode("utf-8"), AES.MODE_CBC, iv.encode("utf-8"))
    return unpad(cipher.decrypt(b64decode(cipher_text)), AES.block_size).decode("utf-8")


class KisOrderWatcher:
    """
    KIS H0STCNI0 체결통보 WebSocket 리스너.
    CNTG_YN=2(체결) + RFUS_YN=N(거부아님) 메시지만 on_fill 콜백으로 전달.
    AES-CBC 복호화 key/iv는 구독 확인 응답 body.output에서 자동 수신.
    """

    def __init__(self, account_no: str, approval_key: str, on_fill):
        self._account_no   = account_no    # "44197559"
        self._approval_key = approval_key
        self._on_fill      = on_fill
        self._iv           = None
        self._key          = None
        self._ws           = None
        self._running      = False

    def start(self):
        self._running = True
        t = threading.Thread(
            target=self._run, daemon=True,
            name=f"KisWatcher-{self._account_no}"
        )
        t.start()
        logger.info("KIS 체결통보 WebSocket 시작 (계좌: %s)", self._account_no)

    def stop(self):
        self._running = False
        if self._ws:
            self._ws.close()

    def _is_trading_hours(self) -> bool:
        now = datetime.now(KST)
        return now.weekday() < 5 and 8 <= now.hour < 20

    def _run(self):
        while self._running:
            if not self._is_trading_hours():
                time.sleep(60)
                continue
            try:
                self._ws = websocket.WebSocketApp(
                    WS_URL,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=lambda ws, e: logger.warning("KIS WS 오류: %s", e),
                    on_close=lambda ws, c, m: logger.info(
                        "KIS WS 연결 종료 (계좌: %s)", self._account_no
                    ),
                )
                self._ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as e:
                logger.warning("KIS WS 예외 — 10초 후 재연결: %s", e)
            if self._running:
                time.sleep(10)

    def _on_open(self, ws):
        msg = {
            "header": {
                "approval_key": self._approval_key,
                "custtype": "P",
                "tr_type": "1",
                "content-type": "utf-8",
            },
            "body": {
                "input": {
                    "tr_id": "H0STCNI0",
                    "tr_key": self._account_no,
                }
            },
        }
        ws.send(json.dumps(msg))
        logger.info("H0STCNI0 구독 요청 전송 (계좌: %s)", self._account_no)

    def _on_message(self, ws, message):
        if not message:
            return

        if message[0] in ("0", "1"):
            # 실시간 데이터 메시지
            parts = message.split("|")
            if len(parts) < 4 or parts[1] != "H0STCNI0":
                return
            raw = parts[3]
            if self._key and self._iv:
                try:
                    raw = _aes_dec(self._key, self._iv, raw)
                except Exception as e:
                    logger.warning("체결통보 복호화 실패: %s", e)
                    return
            fields = raw.split("^")
            if len(fields) < len(FILL_COLUMNS):
                return
            data = dict(zip(FILL_COLUMNS, fields))
            # 체결통보(CNTG_YN=2)이고 거부 아닌 것(RFUS_YN=N)만 처리
            if data.get("CNTG_YN") == "2" and data.get("RFUS_YN") == "N":
                fill = {
                    "order_no":   data.get("ODER_NO", ""),
                    "code":       data.get("STCK_SHRN_ISCD", ""),
                    "action":     "BUY" if data.get("SELN_BYOV_CLS") == "02" else "SELL",
                    "fill_qty":   int(data.get("CNTG_QTY", 0) or 0),
                    "fill_price": int(data.get("CNTG_UNPR", 0) or 0),
                    "fill_time":  data.get("STCK_CNTG_HOUR", ""),
                }
                logger.info(
                    "체결 확인 %s %s %d주 @%d원 (주문: %s)",
                    fill["action"], fill["code"],
                    fill["fill_qty"], fill["fill_price"], fill["order_no"],
                )
                try:
                    self._on_fill(fill)
                except Exception as e:
                    logger.warning("on_fill 콜백 오류: %s", e)
        else:
            # 시스템 메시지 (구독 확인, PINGPONG 등)
            try:
                rsp = json.loads(message)
                tr_id = rsp.get("header", {}).get("tr_id", "")
                if tr_id == "PINGPONG":
                    ws.send(message)
                    return
                output = rsp.get("body", {}).get("output", {})
                if output:
                    self._iv  = output.get("iv")
                    self._key = output.get("key")
                    logger.info("체결통보 암호화 키 수신 완료 (계좌: %s)", self._account_no)
            except Exception:
                pass
