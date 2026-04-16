#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
서버 보수 에이전트 — Qwen3.5 27B (LM Studio) + 텔레그램 worker 봇
- worker 봇 토큰으로 메시지 수신
- Qwen(LM Studio :8000)으로 분석 → 텔레그램 응답
- 파일읽기 / bash 실행 도구 내장
- 작업 완료 후 Gemma4(llm_client.py)로 복귀 안내
"""

import os
import subprocess
import logging
import requests
import time
import json

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

# ── 설정 ──────────────────────────────────────────────────────────────────────
WORKER_TOKEN  = "8634656301:AAGt2g90XCsYoOWedumeBNLHaFpESapq33w"
CHAT_ID       = "8448138406"
LM_STUDIO_URL = "http://221.144.111.116:8000/v1/chat/completions"
QWEN_MODEL    = "qwen3.5-27b-claude-4.6-opus-reasoning-distilled"
WORKSPACE     = "/home/ubuntu/-claude-test-"

SYSTEM_PROMPT = """너는 서버 보수 협업 조수(assistant)다. 반드시 한국어로 답변한다.

역할:
- 로그 분석, 버그 후보 추출, 코드 리뷰, 문제 진단
- 파일 읽기와 bash 명령 실행으로 직접 조사
- 버그 수정, 코드 개선 등 파일 직접 수정
- Claude(설계자)에게 전달할 보고서 작성

도구 사용 형식 (필요시):
{"tool": "read_file", "path": "/절대/경로"}
{"tool": "bash", "cmd": "명령어"}
{"tool": "write_file", "path": "/절대/경로", "content": "전체 파일 내용"}

주요 파일:
- 서버 로그: /home/ubuntu/-claude-test-/proxy_v54.log
- 메인 서버: /home/ubuntu/-claude-test-/proxy_v54.py
- 자동매매: /home/ubuntu/-claude-test-/auto_trader.py
- KIS 클라이언트: /home/ubuntu/-claude-test-/mock_trading/kis_client.py
- KY 클라이언트: /home/ubuntu/-claude-test-/mock_trading/kis_client_ky.py

파일 수정 규칙:
- 수정 전 반드시 read_file로 현재 내용 확인
- 수정 후 변경 내용을 명확히 보고 (몇 번째 줄, 무엇을 바꿨는지)
- git commit은 하지 말 것 — Claude가 검토 후 직접 커밋

절대 금지:
- REAL_TRADE 값 변경 (kis_client.py: False 유지, kis_client_ky.py: True 유지)
- ai_chat.py pre-injection 블록(3-1, 3-2, 3-3) 제거/수정
- 서비스 재시작 (보고만 할 것)
- 확실하지 않으면 수정하지 말고 보고만 할 것

단순 조회(파일읽기·로그확인)는 추론 없이 즉시 처리.
버그분석·코드리뷰·수정에만 깊은 추론 사용."""


# ── 도구 실행 ─────────────────────────────────────────────────────────────────
def _run_tool(tool_call: dict) -> str:
    tool = tool_call.get("tool", "")
    if tool == "read_file":
        path = tool_call.get("path", "")
        try:
            with open(path) as f:
                lines = f.readlines()
            # 마지막 200줄만 (로그 파일 대용량 대응)
            if len(lines) > 200:
                return f"[앞부분 생략, 마지막 200줄]\n" + "".join(lines[-200:])
            return "".join(lines)
        except Exception as e:
            return f"파일 읽기 실패: {e}"

    elif tool == "bash":
        cmd = tool_call.get("cmd", "")
        # 위험 명령 차단
        blocked = ["rm ", "sudo systemctl", "> /", "mkfs", "dd ", "shutdown", "reboot"]
        if any(b in cmd for b in blocked):
            return f"차단된 명령: {cmd}"
        try:
            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True,
                timeout=15, cwd=WORKSPACE
            )
            out = result.stdout[-3000:] if result.stdout else ""
            err = result.stderr[-1000:] if result.stderr else ""
            return (out + ("\n[stderr]\n" + err if err else "")).strip() or "(출력 없음)"
        except subprocess.TimeoutExpired:
            return "명령 타임아웃 (15초)"
        except Exception as e:
            return f"명령 실행 실패: {e}"

    elif tool == "write_file":
        path = tool_call.get("path", "")
        content = tool_call.get("content", "")
        if not path or not content:
            return "write_file: path 또는 content 누락"
        # 절대경로 강제
        if not path.startswith("/"):
            return f"절대경로 필요: {path}"
        # 위험 내용 차단
        PROTECTED = [
            ("REAL_TRADE = True",    "kis_client_ky.py의 REAL_TRADE=True는 수정 금지"),
            ("REAL_TRADE=True",      "kis_client_ky.py의 REAL_TRADE=True는 수정 금지"),
            ("REAL_TRADE = False",   "kis_client.py의 REAL_TRADE=False는 수정 금지 (kis_client_ky.py에 False 추가 시도 차단)"),
        ]
        # kis_client_ky.py에 REAL_TRADE=False 쓰기 차단
        if "kis_client_ky" in path and "REAL_TRADE = False" in content:
            return "차단: kis_client_ky.py에 REAL_TRADE=False 설정 금지 (실전 계좌)"
        # kis_client.py(비KY)에 REAL_TRADE=True 쓰기 차단
        if "kis_client_ky" not in path and "kis_client" in path and "REAL_TRADE = True" in content:
            return "차단: kis_client.py에 REAL_TRADE=True 설정 금지 (가상계좌)"
        # pre-injection 블록 삭제 차단 (ai_chat.py)
        if "ai_chat" in path:
            for marker in ["3-1)", "3-2)", "3-3)", "pre-injection"]:
                if marker not in content:
                    return f"차단: ai_chat.py에서 pre-injection 블록({marker}) 제거 금지"
        # 백업 생성
        backup_path = path + ".bak"
        try:
            with open(path) as f:
                old_content = f.read()
            with open(backup_path, "w") as f:
                f.write(old_content)
        except FileNotFoundError:
            old_content = ""
        except Exception as e:
            return f"백업 실패: {e}"
        # 파일 쓰기
        try:
            with open(path, "w") as f:
                f.write(content)
        except Exception as e:
            return f"파일 쓰기 실패: {e}"
        # 변경 라인 수 계산
        old_lines = old_content.splitlines()
        new_lines = content.splitlines()
        added   = len(new_lines) - len(old_lines)
        sign    = f"+{added}" if added >= 0 else str(added)
        return (f"✅ 파일 수정 완료: {path}\n"
                f"변경: {len(old_lines)}줄 → {len(new_lines)}줄 ({sign})\n"
                f"백업: {backup_path}")

    return f"알 수 없는 도구: {tool}"


# ── LM Studio 호출 ─────────────────────────────────────────────────────────────
def call_qwen(user_msg: str) -> str:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user",   "content": user_msg},
    ]
    for _round in range(5):  # 도구 호출 최대 5라운드
        try:
            r = requests.post(
                LM_STUDIO_URL,
                json={"model": QWEN_MODEL, "messages": messages,
                      "temperature": 0.3, "max_tokens": 4096},
                timeout=(5, 180),
            )
            r.raise_for_status()
            data    = r.json()
            content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
            if not content:
                return "⚠️ Qwen 응답 없음"

            # 도구 호출 감지
            tool_call = None
            for line in content.splitlines():
                line = line.strip()
                if line.startswith('{"tool"'):
                    try:
                        tool_call = json.loads(line)
                        break
                    except Exception:
                        pass

            if not tool_call:
                return content  # 최종 답변

            # 도구 실행 후 결과 주입
            tool_result = _run_tool(tool_call)
            logger.info("도구 실행: %s → %d chars", tool_call.get("tool"), len(tool_result))
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user",
                             "content": f"[도구 결과: {tool_call.get('tool')}]\n{tool_result}\n\n위 결과를 바탕으로 분석 결과를 한국어로 답해줘."})

        except Exception as e:
            logger.error("Qwen 호출 실패: %s", e)
            return f"⚠️ Qwen 연결 실패: {e}"

    return "⚠️ 도구 호출 루프 한도 초과"


# ── 텔레그램 ───────────────────────────────────────────────────────────────────
def tg_send(text: str):
    for chunk in [text[i:i+4000] for i in range(0, len(text), 4000)]:
        try:
            requests.post(
                f"https://api.telegram.org/bot{WORKER_TOKEN}/sendMessage",
                data={"chat_id": CHAT_ID, "text": chunk},
                timeout=10,
            )
        except Exception as e:
            logger.error("텔레그램 전송 실패: %s", e)


def tg_poll(offset: int) -> tuple:
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{WORKER_TOKEN}/getUpdates",
            params={"offset": offset, "timeout": 30, "allowed_updates": ["message"]},
            timeout=35,
        )
        updates = r.json().get("result", [])
        return updates, (updates[-1]["update_id"] + 1 if updates else offset)
    except Exception:
        return [], offset


# ── 메인 루프 ─────────────────────────────────────────────────────────────────
def main():
    logger.info("서버보수에이전트 시작 (Qwen → worker 봇)")
    tg_send("🔧 서버보수에이전트 시작됨\nQwen3.5-27B 연결 완료. 분석 요청을 보내주세요.")
    offset = 0
    while True:
        updates, offset = tg_poll(offset)
        for upd in updates:
            msg  = upd.get("message", {})
            text = msg.get("text", "").strip()
            from_id = str(msg.get("from", {}).get("id", ""))
            if not text or from_id != CHAT_ID:
                continue
            if text.lower() in ("/start", "/help"):
                tg_send("🔧 서버보수에이전트\n로그 분석·버그 진단·코드 리뷰 요청을 보내주세요.\n종료: /exit")
                continue
            if text.lower() == "/exit":
                tg_send("👋 서버보수에이전트 종료. Gemma4(자동매매)로 복귀합니다.")
                logger.info("사용자 요청으로 종료")
                return
            logger.info("요청: %s", text[:80])
            tg_send("⏳ 분석 중...")
            reply = call_qwen(text)
            tg_send(reply)
            logger.info("응답 완료: %d chars", len(reply))


if __name__ == "__main__":
    main()
