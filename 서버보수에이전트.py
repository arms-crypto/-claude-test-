#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
서버 보수 에이전트 — Qwen3.5 27B (LM Studio) + 텔레그램 worker 봇
- worker 봇 토큰으로 메시지 수신 (사용자 ↔ Qwen 텔레그램 대화)
- POST http://localhost:8001/task → Claude가 직접 작업 지시
- Qwen(LM Studio :8000)으로 분석 → 텔레그램 응답
- 파일읽기 / bash 실행 / 파일 수정 도구 내장
"""

import os
import re
import subprocess
import logging
import requests
import time
import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

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
TASK_PORT     = 8001

def _load_system_prompt() -> str:
    claude_md_path = os.path.join(WORKSPACE, "CLAUDE.md")
    try:
        with open(claude_md_path, encoding="utf-8") as f:
            claude_md = f.read()
        claude_md_section = f"\n\n---\n# 프로젝트 컨텍스트 (CLAUDE.md)\n{claude_md}\n---"
    except Exception as e:
        logger.warning("CLAUDE.md 로드 실패: %s", e)
        claude_md_section = ""

    return """너는 서버 보수 협업 조수(assistant)다. 반드시 한국어로 답변한다.

# 작업 환경
WORKSPACE = /home/ubuntu/-claude-test-
모든 파일 경로는 이 WORKSPACE 기준으로 절대경로 사용:
  예) /home/ubuntu/-claude-test-/mock_trading/kis_client.py

# 도구 사용법 (XML 태그 방식)

## 1. 파일 읽기
<read_file path="/home/ubuntu/-claude-test-/파일경로"/>
<read_file path="/home/ubuntu/-claude-test-/파일경로" limit_lines="50"/>              ← 앞 50줄
<read_file path="/home/ubuntu/-claude-test-/파일경로" limit_lines="30" offset="20"/>  ← 21~50줄

## 2. 특정 텍스트 교체 [★ 최우선 — 파일 일부만 바꿀 때 반드시 사용]
<replace_text path="/home/ubuntu/-claude-test-/파일경로" old="바꿀 원본 텍스트" new="새 텍스트"/>

## 3. bash (조회 + sed -i 허용)
<bash>grep -n "변수명" /home/ubuntu/-claude-test-/파일경로</bash>
<bash>sed -i 's/이전값/새값/' /home/ubuntu/-claude-test-/파일경로</bash>

## 4. 전체 파일 쓰기 [전체 내용을 알 때만]
<write_file path="/home/ubuntu/-claude-test-/파일경로">
파일 전체 내용
</write_file>

# 파일 수정 최적 워크플로우
## 파일 일부 수정 (권장 순서):
1. grep으로 해당 줄 정확한 텍스트 확인: <bash>grep -n "키워드" /경로</bash>
2. replace_text로 교체: <replace_text path="..." old="정확한텍스트" new="새텍스트"/>
3. 완료 보고

## 긴 파일 읽기 (200줄 초과 시):
- 전체 재읽기 금지 — limit_lines로 필요한 부분만 읽기
- 수정 타겟이 앞부분이면: <read_file path="..." limit_lines="50"/>

# 파일 수정 예시
사용자: config.py에서 DEBUG = False → True 로 변경해줘

[grep으로 정확한 텍스트 확인]
<bash>grep -n "DEBUG" /home/ubuntu/-claude-test-/config.py</bash>

[결과 확인 후 replace_text 실행]
<replace_text path="/home/ubuntu/-claude-test-/config.py" old="DEBUG = False" new="DEBUG = True"/>

✅ 수정 완료

# 에러 발생 시 규칙
- 경로 에러: bash로 1회 확인 후 즉시 수정 진행
- 같은 파일 read_file 2회 이상 금지 — limit_lines로 범위 좁혀 읽기
- replace_text 실패 시: grep으로 정확한 텍스트 재확인 후 재시도, 그래도 실패 시 sed -i 사용

# 금지
- git commit (Claude가 검토 후 직접 커밋)
- 서비스 재시작 (보고만 할 것)
- bash로 파일 수정 (sed, awk, tee, echo redirect)
""" + claude_md_section


SYSTEM_PROMPT = _load_system_prompt()


# ── 도구 호출 파싱 (XML 태그 우선, JSON 폴백) ────────────────────────────────
def _parse_tool_call(content: str) -> dict | None:
    # read_file: <read_file path="..." limit_lines="50" offset="0"/>
    m = re.search(r'<read_file\s+([^/>]+)', content)
    if m:
        attrs = m.group(1)
        path  = re.search(r'path=["\']([^"\']+)["\']', attrs)
        limit = re.search(r'limit_lines=["\']?(\d+)["\']?', attrs)
        off   = re.search(r'offset=["\']?(\d+)["\']?', attrs)
        if path:
            result = {"tool": "read_file", "path": path.group(1)}
            if limit:
                result["limit_lines"] = int(limit.group(1))
            if off:
                result["offset"] = int(off.group(1))
            return result

    # replace_text: <replace_text path="..." old="..." new="..."/>  ← 핵심 도구
    m = re.search(r'<replace_text\s+path=["\']([^"\']+)["\']\s+old=["\']([^"\']*)["\']'
                  r'\s+new=["\']([^"\']*)["\']', content)
    if m:
        return {"tool": "replace_text", "path": m.group(1), "old": m.group(2), "new": m.group(3)}
    # 멀티라인 old/new 지원
    m = re.search(r'<replace_text\s+path=["\']([^"\']+)["\']>(.*?)<old>(.*?)</old>\s*<new>(.*?)</new>.*?</replace_text>',
                  content, re.DOTALL)
    if m:
        return {"tool": "replace_text", "path": m.group(1), "old": m.group(3), "new": m.group(4)}

    # write_file: <write_file path="...">content</write_file>
    m = re.search(r'<write_file\s+path=["\']([^"\']+)["\']>(.*?)</write_file>', content, re.DOTALL)
    if m:
        return {"tool": "write_file", "path": m.group(1), "content": m.group(2)}

    # bash: <bash>cmd</bash>
    m = re.search(r'<bash>(.*?)</bash>', content, re.DOTALL)
    if m:
        return {"tool": "bash", "cmd": m.group(1).strip()}

    # JSON 폴백 (이전 방식)
    for line in content.splitlines():
        line = line.strip()
        if line.startswith('{"tool"'):
            try:
                return json.loads(line)
            except Exception:
                pass

    return None


# ── 도구 실행 ─────────────────────────────────────────────────────────────────
def _run_tool(tool_call: dict) -> str:
    tool = tool_call.get("tool", "")
    if tool == "read_file":
        path       = tool_call.get("path", "")
        limit      = tool_call.get("limit_lines", None)
        offset     = tool_call.get("offset", 0)  # 시작 줄 (0-based)
        try:
            with open(path, encoding="utf-8") as f:
                lines = f.readlines()
            total = len(lines)
            if limit is not None:
                # 범위 지정 읽기
                start = int(offset)
                end   = start + int(limit)
                chunk = lines[start:end]
                return f"[줄 {start+1}~{min(end, total)}/{total}]\n" + "".join(chunk)
            if total > 200:
                return f"[앞부분 생략, 마지막 200줄 — 앞부분은 limit_lines/offset 사용]\n" + "".join(lines[-200:])
            return "".join(lines)
        except Exception as e:
            return f"파일 읽기 실패: {e}"

    elif tool == "bash":
        cmd = tool_call.get("cmd", "")
        logger.info("bash 명령: %s", cmd[:200])  # 명령 로깅
        blocked = ["rm ", "sudo systemctl", "> /", "mkfs", "dd ", "shutdown", "reboot",
                   "tee ", "> /"]  # sed -i는 허용 (replace_text 실패 시 대안)
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

    elif tool == "replace_text":
        path    = tool_call.get("path", "")
        old_str = tool_call.get("old", "")
        new_str = tool_call.get("new", "")
        if not path or old_str == "":
            return "replace_text: path 또는 old 누락"
        if not path.startswith("/"):
            return f"절대경로 필요: {path}"
        try:
            with open(path, encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            return f"파일 읽기 실패: {e}"
        if old_str not in content:
            # 폴백 1: 주석 제거 후 코드 부분만 매칭
            old_code = old_str.split('#')[0].rstrip()
            matched_line = None
            for line in content.splitlines():
                if old_code and line.split('#')[0].rstrip() == old_code:
                    matched_line = line
                    break
            # 폴백 2: 공백 정규화 매칭
            if matched_line is None:
                import re as _re
                norm_old = _re.sub(r'\s+', ' ', old_str).strip()
                for line in content.splitlines():
                    if _re.sub(r'\s+', ' ', line).strip() == norm_old:
                        matched_line = line
                        break
            if matched_line is not None:
                old_str = matched_line
            else:
                return f"교체 실패: '{old_str[:80]}' 를 파일에서 찾을 수 없음"
        backup_path = path + ".bak"
        with open(backup_path, "w", encoding="utf-8") as f:
            f.write(content)
        new_content = content.replace(old_str, new_str, 1)
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_content)
        return f"✅ 교체 완료: {path}\n'{old_str[:60]}' → '{new_str[:60]}'\n백업: {backup_path}"

    elif tool == "write_file":
        path = tool_call.get("path", "")
        content = tool_call.get("content", "")
        if not path or not content:
            return "write_file: path 또는 content 누락"
        if not path.startswith("/"):
            return f"절대경로 필요: {path}"
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
        try:
            with open(path, "w") as f:
                f.write(content)
        except Exception as e:
            return f"파일 쓰기 실패: {e}"
        old_lines = old_content.splitlines()
        new_lines = content.splitlines()
        added = len(new_lines) - len(old_lines)
        sign  = f"+{added}" if added >= 0 else str(added)
        return (f"✅ 파일 수정 완료: {path}\n"
                f"변경: {len(old_lines)}줄 → {len(new_lines)}줄 ({sign})\n"
                f"백업: {backup_path}")

    return f"알 수 없는 도구: {tool}"


# ── 세션 히스토리 ─────────────────────────────────────────────────────────────
_sessions: dict = {}          # session_id → [{"role": ..., "content": ...}, ...]
MAX_HISTORY = 10              # 유지할 최대 대화 턴 수 (user+assistant 쌍)


def _get_history(session_id: str) -> list:
    return _sessions.get(session_id, [])


def _append_history(session_id: str, role: str, content: str):
    if session_id not in _sessions:
        _sessions[session_id] = []
    _sessions[session_id].append({"role": role, "content": content})
    # 오래된 히스토리 정리 (system 제외, user+assistant 쌍 기준)
    if len(_sessions[session_id]) > MAX_HISTORY * 2:
        _sessions[session_id] = _sessions[session_id][-(MAX_HISTORY * 2):]


def reset_history(session_id: str):
    _sessions.pop(session_id, None)


# ── LM Studio 호출 ─────────────────────────────────────────────────────────────
def call_qwen(user_msg: str, session_id: str = "default") -> str:
    # 히스토리 포함 메시지 구성
    history = _get_history(session_id)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history + [
        {"role": "user", "content": user_msg}
    ]

    final_reply = ""
    for _round in range(5):
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

            logger.info("Qwen 응답 내용: %s", content[:300])
            tool_call = _parse_tool_call(content)

            if not tool_call:
                final_reply = content
                break

            tool_result = _run_tool(tool_call)
            tool_name = tool_call.get("tool")
            logger.info("도구 실행: %s → %d chars", tool_name, len(tool_result))

            # 도구별 맞춤 피드백
            if tool_name == "read_file":
                next_step = "파일 내용 확인 완료. 이제 <write_file> 태그로 수정된 전체 파일 내용을 작성하여 저장하세요. 더 이상 read_file을 반복하지 말고 바로 write_file을 사용하세요."
            elif tool_name == "write_file":
                next_step = "파일 저장 완료. 변경 내용을 한국어로 보고하세요."
            else:
                next_step = "명령 완료. 다음 작업을 진행하세요."

            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user",
                             "content": f"[도구 결과: {tool_name}]\n{tool_result}\n\n{next_step}"})

        except Exception as e:
            logger.error("Qwen 호출 실패: %s", e)
            return f"⚠️ Qwen 연결 실패: {e}"
    else:
        return "⚠️ 도구 호출 루프 한도 초과"

    # 히스토리에 이번 대화 저장
    _append_history(session_id, "user", user_msg)
    _append_history(session_id, "assistant", final_reply)
    return final_reply


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


# ── HTTP 태스크 서버 (Claude → Qwen 직접 지시) ────────────────────────────────
def _process_task(task_text: str):
    """백그라운드에서 Qwen 호출 후 텔레그램으로 결과 전송"""
    logger.info("[Claude→Qwen] 작업 수신: %s", task_text[:80])
    tg_send(f"📋 Claude 작업지시 수신:\n{task_text[:200]}\n\n⏳ 처리 중...")
    reply = call_qwen(task_text)
    tg_send(f"✅ 작업 완료:\n{reply}")
    logger.info("[Claude→Qwen] 작업 완료: %d chars", len(reply))


class TaskHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/task":
            self.send_response(404)
            self.end_headers()
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body   = self.rfile.read(length).decode("utf-8")
            data   = json.loads(body)
            task   = data.get("task", "").strip()
            if not task:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'{"error": "task field required"}')
                return
            # 백그라운드 처리 (응답 먼저 반환)
            threading.Thread(target=_process_task, args=(task,), daemon=True).start()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "accepted", "task": task[:100]}).encode())
        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def log_message(self, format, *args):
        logger.info("[TaskServer] " + format % args)


def start_task_server():
    server = HTTPServer(("127.0.0.1", TASK_PORT), TaskHandler)
    logger.info("Claude→Qwen 태스크 서버 시작: http://127.0.0.1:%d/task", TASK_PORT)
    server.serve_forever()


# ── 메인 루프 ─────────────────────────────────────────────────────────────────
def main():
    # HTTP 태스크 서버 백그라운드 시작
    threading.Thread(target=start_task_server, daemon=True).start()

    logger.info("서버보수에이전트 시작 (Qwen → worker 봇)")
    tg_send("🔧 서버보수에이전트 시작됨\nQwen3.5-27B 연결 완료.\n\n• 텔레그램으로 직접 대화 가능\n• Claude 자동 작업지시: localhost:8001/task")
    offset = 0
    while True:
        updates, offset = tg_poll(offset)
        for upd in updates:
            msg     = upd.get("message", {})
            text    = msg.get("text", "").strip()
            from_id = str(msg.get("from", {}).get("id", ""))
            if not text or from_id != CHAT_ID:
                continue
            if text.lower() in ("/start", "/help"):
                tg_send("🔧 서버보수에이전트\n로그 분석·버그 진단·코드 리뷰 요청을 보내주세요.\n/reset — 대화 초기화\n종료: /exit")
                continue
            if text.lower() == "/reset":
                reset_history(CHAT_ID)
                tg_send("🔄 대화 히스토리 초기화됨")
                continue
            if text.lower() == "/exit":
                tg_send("👋 서버보수에이전트 종료.")
                logger.info("사용자 요청으로 종료")
                return
            logger.info("요청: %s", text[:80])
            tg_send("⏳ 분석 중...")
            reply = call_qwen(text, session_id=CHAT_ID)
            tg_send(reply)
            logger.info("응답 완료: %d chars", len(reply))


if __name__ == "__main__":
    main()
