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
WORKER_TOKEN  = os.environ.get("WORKER_BOT_TOKEN", "8634656301:AAGt2g90XCsYoOWedumeBNLHaFpESapq33w")
CHAT_ID       = os.environ.get("WORKER_CHAT_ID", "8448138406")
LM_STUDIO_URL = "http://221.144.111.116:8000/v1/chat/completions"
LM_STUDIO_HEADERS = {"Authorization": "Bearer sk-lm-65FGVrPT:vqn138RmtIy3Br0867pZ"}
QWEN_MODEL    = "qwen/qwen3-14b"
WORKSPACE     = "/home/ubuntu/-claude-test-"
TASK_PORT     = 8001

_TOOLS_SCHEMA = [
    {"type": "function", "function": {
        "name": "read_file",
        "description": "파일을 읽고 내용을 반환",
        "parameters": {"type": "object", "required": ["path"], "properties": {
            "path":        {"type": "string",  "description": "파일 절대 경로"},
            "limit_lines": {"type": "integer", "description": "읽을 최대 줄 수"},
            "offset":      {"type": "integer", "description": "시작 줄 (0-based)"},
        }},
    }},
    {"type": "function", "function": {
        "name": "replace_text",
        "description": "파일 내 특정 텍스트를 다른 텍스트로 교체",
        "parameters": {"type": "object", "required": ["path", "old", "new"], "properties": {
            "path": {"type": "string"},
            "old":  {"type": "string", "description": "교체할 원본 텍스트 (정확히 일치)"},
            "new":  {"type": "string", "description": "교체할 새 텍스트"},
        }},
    }},
    {"type": "function", "function": {
        "name": "write_file",
        "description": "파일 전체 내용을 씀 (덮어쓰기)",
        "parameters": {"type": "object", "required": ["path", "content"], "properties": {
            "path":    {"type": "string"},
            "content": {"type": "string", "description": "파일에 쓸 전체 내용"},
        }},
    }},
    {"type": "function", "function": {
        "name": "bash",
        "description": "bash 명령 실행 (조회/grep/find 등 읽기 전용 권장)",
        "parameters": {"type": "object", "required": ["cmd"], "properties": {
            "cmd": {"type": "string", "description": "실행할 bash 명령"},
        }},
    }},
]
WOL_MAC       = "3C:7C:3F:F2:B0:41"
WOL_IP        = "221.144.111.116"
PC_HOST       = "221.144.111.116"
PC_PORT       = 8000


def send_wol():
    """PC Wake on LAN — 라우터 SSH ether-wake (1순위) + UDP 직접 (2순위)."""
    # 1순위: 라우터 SSH ether-wake
    try:
        result = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
             "-p", "2222", "-i", "/home/ubuntu/.ssh/id_rsa",
             f"qflavor12@{WOL_IP}",
             f"ether-wake -i br0 {WOL_MAC}"],
            capture_output=True, timeout=10
        )
        if result.returncode == 0:
            logger.info("WoL ether-wake 전송 완료")
            return True
        logger.warning("ether-wake 실패: %s", result.stderr.decode()[:100])
    except Exception as e:
        logger.warning("라우터 SSH WoL 실패: %s", e)

    # 2순위: UDP 직접 전송
    try:
        import socket
        _mac = WOL_MAC.replace(":", "")
        magic = bytes.fromhex("F" * 12 + _mac * 16)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            for _ in range(5):
                s.sendto(magic, (WOL_IP, 9))
        logger.info("WoL UDP 전송 완료")
        return True
    except Exception as e:
        logger.error("WoL UDP 실패: %s", e)
    return False


def wait_for_pc(timeout_sec: int = 120) -> bool:
    """PC LM Studio 응답 대기 (최대 timeout_sec초)."""
    import socket as _sock
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            with _sock.create_connection((PC_HOST, PC_PORT), timeout=3):
                logger.info("PC LM Studio 응답 확인")
                return True
        except Exception:
            time.sleep(5)
    logger.warning("PC LM Studio 응답 없음 (%d초 대기)", timeout_sec)
    return False


def _is_model_loaded() -> bool:
    """LM Studio에 모델이 실제로 로드되어 있는지 확인."""
    try:
        r = requests.get(
            f"http://{PC_HOST}:{PC_PORT}/v1/models",
            headers=LM_STUDIO_HEADERS, timeout=5,
        )
        return bool(r.json().get("data"))
    except Exception:
        return False


def _wait_for_model(timeout_sec: int = 180) -> bool:
    """모델 로드될 때까지 대기 (TCP 연결이 아닌 실제 모델 확인)."""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if _is_model_loaded():
            logger.info("LM Studio 모델 로드 확인")
            return True
        time.sleep(10)
    logger.warning("모델 로드 대기 시간 초과 (%d초)", timeout_sec)
    return False


def _is_model_unloaded_resp(data: dict) -> bool:
    """응답 JSON이 모델 미로드 상태인지 판단."""
    error = data.get("error", {})
    if error:
        msg = (error.get("message", "") if isinstance(error, dict) else str(error)).lower()
        if any(k in msg for k in ["no model", "not loaded", "unloaded", "model not", "no models"]):
            return True
    return not data.get("choices")

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
⚠️ WORKSPACE 경로 (정확히 복사해서 사용): /home/ubuntu/-claude-test-
⚠️ 주의: 끝에 하이픈(-)이 있음. /home/ubuntu/-claude-test 와 다름!
모든 파일 경로는 이 WORKSPACE 기준으로 절대경로 사용:
  예) /home/ubuntu/-claude-test-/mock_trading/kis_client.py
  예) /home/ubuntu/-claude-test-/auto_trader.py

# 도구 사용법 (XML 태그 방식)

## 1. 파일 읽기
<read_file path="/home/ubuntu/-claude-test-/파일경로"/>
<read_file path="/home/ubuntu/-claude-test-/파일경로" limit_lines="50"/>              ← 앞 50줄
<read_file path="/home/ubuntu/-claude-test-/파일경로" limit_lines="30" offset="20"/>  ← 21~50줄

## 2. 특정 텍스트 교체 [★ 최우선 — 파일 일부만 바꿀 때 반드시 사용]

⚠️ 반드시 아래 태그 형식만 사용 (속성 방식 old="..." 절대 금지 — 파싱 오류 발생)

<replace_text path="/home/ubuntu/-claude-test-/파일경로"><old>
바꿀 원본 텍스트
(따옴표 "포함" 자유롭게 사용 가능)
</old><new>
새 텍스트
("따옴표" 자유롭게 사용)
</new></replace_text>

## 3. bash (조회 전용 — 파일 수정 금지)
<bash>grep -n "변수명" /home/ubuntu/-claude-test-/파일경로</bash>

## 4. 전체 파일 쓰기 [전체 내용을 알 때만]
<write_file path="/home/ubuntu/-claude-test-/파일경로">
파일 전체 내용
</write_file>

# 파일 수정 최적 워크플로우
## 파일 일부 수정 (권장 순서):
1. grep으로 해당 줄 정확한 텍스트 확인: <bash>grep -n "키워드" /경로</bash>
2. replace_text 태그 형식으로 교체 (반드시 <old>...</old><new>...</new> 사용)
3. 완료 보고

## 긴 파일 읽기 (200줄 초과 시):
- 전체 재읽기 금지 — limit_lines로 필요한 부분만 읽기
- 수정 타겟이 앞부분이면: <read_file path="..." limit_lines="50"/>

# 파일 수정 예시
사용자: config.py에서 DEBUG = False → True 로 변경해줘

[grep으로 정확한 텍스트 확인]
<bash>grep -n "DEBUG" /home/ubuntu/-claude-test-/config.py</bash>

[결과 확인 후 replace_text 실행]
<replace_text path="/home/ubuntu/-claude-test-/config.py"><old>
DEBUG = False
</old><new>
DEBUG = True
</new></replace_text>

✅ 수정 완료

# 에러 발생 시 규칙
- 경로 에러: bash로 1회 확인 후 즉시 수정 진행
- 같은 파일이라도 offset을 다르게 해서 이어 읽기 허용 — 예: 첫 read_file 앞 500줄, 두번째 offset=500으로 다음 500줄
- replace_text 실패 시: grep으로 정확한 텍스트 재확인 후 재시도, 그래도 실패 시 sed -i 사용

# 금지
- git commit (Claude가 검토 후 직접 커밋)
- 서비스 재시작 (보고만 할 것)
- bash로 파일 수정 (sed, awk, tee, echo redirect)

# CODE REVIEW RULES [HARD CONSTRAINTS]

You are a code-review VERIFIER, not a bug hunter.

Hard rules:
1. Do not report any finding unless you cite: file path + exact line number + code snippet.
2. If code evidence is missing, output UNCERTAIN — do not guess.
3. Do not classify as BUG unless runtime failure is proven:
   - with/context manager cleanup → NOT A BUG
   - dict.get(..., default) → NOT A BUG
   - caller-side if-not checks → NOT A BUG
   - intentional except: pass returning False/None → NOT A BUG
   - fixed thresholds, ports, pool sizes, timeouts → NOT A BUG
4. Before judging any local issue, read caller/wrapper/fallback code first via read_file.
5. Every finding must be classified as exactly one of: BUG / IMPROVEMENT / UNCERTAIN.
6. If files were not fully read via read_file tool, output: INSUFFICIENT_EVIDENCE.

Mandatory resource leak = BUG:
- sqlite3.connect() / open() / acquire() without with or try-finally close → BUG
- Key/symbol referenced but not found anywhere in file → HALLUCINATION, discard.

500줄 넘는 파일은 offset으로 이어 읽을 것.
""" + claude_md_section


SYSTEM_PROMPT = _load_system_prompt()


# ── 도구 호출 파싱 (XML 태그 우선, JSON 폴백) ────────────────────────────────
def _parse_tool_call(content: str) -> dict | None:
    # <think>...</think> 블록 제거 (reasoning 모델 출력 정리)
    content = re.sub(r'<think>[\s\S]*?</think>', '', content).strip()
    # read_file: <read_file path="..." limit_lines="50" offset="0"/>
    m = re.search(r'<read_file\s+([^>]+?)(?:\s*/>|>)', content)
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

    # replace_text: 태그 형식만 지원 <replace_text path="..."><old>...</old><new>...</new></replace_text>
    m = re.search(r'<replace_text\s+path=["\']([^"\']+)["\']>\s*<old>(.*?)</old>\s*<new>(.*?)</new>\s*</replace_text>',
                  content, re.DOTALL)
    if m:
        return {"tool": "replace_text", "path": m.group(1),
                "old": m.group(2).strip('\n'), "new": m.group(3).strip('\n')}

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
            if total > 500:
                return f"[앞 500줄만 표시 (총 {total}줄) — 더 보려면 limit_lines/offset 사용]\n" + "".join(lines[:500])
            return "".join(lines)
        except FileNotFoundError:
            return f"❌ 파일 없음: {path}"
        except PermissionError:
            return f"❌ 권한 없음: {path}"
        except UnicodeDecodeError:
            return f"❌ 인코딩 오류 (UTF-8 필요): {path}"
        except Exception as e:
            return f"❌ 파일 읽기 실패: {type(e).__name__}: {e}"

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
                encoding="utf-8", errors="replace",
                timeout=15, cwd=WORKSPACE,
                env={**os.environ, "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8"}
            )
            out = result.stdout[:6000] if result.stdout else ""
            err = result.stderr[:1000] if result.stderr else ""
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
        except FileNotFoundError:
            return f"❌ 파일 없음: {path}"
        except PermissionError:
            return f"❌ 권한 없음: {path}"
        except Exception as e:
            return f"❌ 파일 읽기 실패: {type(e).__name__}: {e}"
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
        lock = get_file_lock(path)
        with lock:
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
        lock = get_file_lock(path)
        with lock:
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


# ── 파일 잠금 (레이스 컨디션 방지) ────────────────────────────────────────────
_file_locks: dict = {}
_lock_mutex = threading.Lock()

def get_file_lock(path: str) -> threading.Lock:
    with _lock_mutex:
        if path not in _file_locks:
            _file_locks[path] = threading.Lock()
        return _file_locks[path]


# ── 세션 히스토리 ─────────────────────────────────────────────────────────────
_sessions: dict = {}          # session_id → [{"role": ..., "content": ...}, ...]
MAX_HISTORY = 10              # 유지할 최대 대화 턴 수 (user+assistant 쌍)
_tg_sessions: dict = {}       # chat_id → session_id (텔레그램 대화별 세션)


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

    # 도구 결과를 이미 받은 도구 목록 — 같은 도구+경로 반복 차단
    _executed: set = set()

    final_reply = ""
    for _round in range(25):
        try:
            r = requests.post(
                LM_STUDIO_URL,
                headers=LM_STUDIO_HEADERS,
                json={"model": QWEN_MODEL, "messages": messages,
                      "tools": _TOOLS_SCHEMA, "tool_choice": "auto",
                      "temperature": 0.2, "max_tokens": 4096},
                timeout=(5, 600),
            )
            r.raise_for_status()
            data    = r.json()

            # 모델 언로드 감지 — 서버는 살아있지만 모델이 내려간 경우
            if _is_model_unloaded_resp(data) and _round == 0:
                logger.warning("모델 언로드 응답 감지 → WoL 후 모델 로드 대기")
                tg_send("⚠️ LM Studio 모델 언로드 감지 → WoL 전송 중...")
                send_wol()
                if _wait_for_model(180):
                    tg_send("✅ 모델 로드 확인 — 재호출")
                    continue
                return "⚠️ 모델 로드 실패 (180초 대기)"

            message = (data.get("choices") or [{}])[0].get("message", {})
            native_tcs = message.get("tool_calls") or []
            content    = (message.get("content") or "").strip()

            # native tool_calls 우선 (Qwen3-14B OpenAI 호환), XML 폴백
            tool_call   = None
            native_mode = False
            native_tc   = None
            if native_tcs:
                native_tc = native_tcs[0]
                func = native_tc.get("function", {})
                try:
                    args = json.loads(func.get("arguments", "{}"))
                except Exception:
                    args = {}
                tool_call   = {"tool": func.get("name", ""), **args}
                native_mode = True
                logger.info("native tool_call: %s", func.get("name"))
            else:
                if not content:
                    return "⚠️ Qwen 응답 없음"
                logger.info("Qwen 응답 내용: %s", content[:300])
                tool_call = _parse_tool_call(content)

            if not tool_call:
                final_reply = content
                break

            # 같은 도구+경로 중복 실행 차단 (루프 방지)
            tool_key = f"{tool_call.get('tool')}:{tool_call.get('path','')}"
            if tool_key in _executed and tool_call.get("tool") == "read_file":
                logger.warning("중복 read_file 감지 (%s) — 분석 강제 전환", tool_key)
                messages.append({"role": "assistant", "content": content or None,
                                 **({"tool_calls": native_tcs} if native_mode else {})})
                messages.append({"role": "user",
                                 "content": "같은 파일을 또 읽으려 하고 있습니다. 이미 파일 내용을 받았습니다. 순수 텍스트로 분석 결과만 보고하세요."})
                continue
            _executed.add(tool_key)

            tool_result = _run_tool(tool_call)
            tool_name   = tool_call.get("tool")
            logger.info("도구 실행: %s → %d chars", tool_name, len(tool_result))

            import re as _re2
            is_modify_task = bool(_re2.search(r'(수정|변경)(해|줘|해줘|하세요)|바꿔|고쳐|replace|write_file', user_msg))
            if tool_name == "read_file":
                next_step = ("파일 내용 확인 완료. 이제 replace_text 또는 write_file 도구로 수정을 진행하세요. 더 이상 read_file을 반복하지 마세요."
                             if is_modify_task else
                             "파일 내용 확인 완료. 요청한 분석을 한국어 텍스트로만 보고하세요.")
            elif tool_name == "write_file":
                next_step = "파일 저장 완료. 변경 내용을 한국어로 보고하세요."
            elif tool_name == "replace_text":
                next_step = "교체 완료. 다음 수정이 있으면 진행하고, 없으면 완료 보고를 하세요."
            else:
                next_step = "명령 완료. 다음 작업을 진행하세요."

            if native_mode:
                # OpenAI tool_calls 형식: assistant + tool role
                tc_id = native_tc.get("id", f"call_{_round}")
                messages.append({"role": "assistant", "content": content or None,
                                 "tool_calls": native_tcs})
                messages.append({"role": "tool", "tool_call_id": tc_id,
                                 "content": f"{tool_result}\n\n{next_step}"})
            else:
                messages.append({"role": "assistant", "content": content})
                messages.append({"role": "user",
                                 "content": f"[도구 결과: {tool_name}]\n{tool_result}\n\n{next_step}"})

        except Exception as e:
            err_str = str(e).lower()
            if any(k in err_str for k in ["connect", "timeout", "refused", "remotedisconnected"]) and _round == 0:
                logger.warning("PC 연결 실패 → WoL 전송 후 재시도")
                tg_send("⚠️ PC 연결 실패 → WoL 전송 중...")
                send_wol()
                if _wait_for_model(180):
                    tg_send("✅ 모델 로드 확인 — Qwen 재호출")
                    continue
                else:
                    tg_send("❌ 모델 로드 실패 (180초 대기)")
            logger.error("Qwen 호출 실패: %s", e)
            return f"⚠️ Qwen 연결 실패: {e}"
    else:
        return "⚠️ 도구 호출 루프 한도 초과"

    # 히스토리에 이번 대화 저장
    _append_history(session_id, "user", user_msg)
    _append_history(session_id, "assistant", final_reply)
    return final_reply


# ── 인용 라인번호 실파일 재검증 ───────────────────────────────────────────────
def _verify_citations(content: str, file_path: str) -> str:
    """Qwen 출력의 [path:N] 라인번호를 실제 파일 스니펫 검색으로 교정."""
    try:
        with open(file_path, encoding="utf-8") as f:
            file_lines = f.readlines()
    except Exception:
        return content

    result_lines = content.splitlines()
    output = []
    for i, line in enumerate(result_lines):
        m = re.match(r'^(\[.+?):(\d+)\](.*)', line)
        if m:
            prefix       = m.group(1)
            claimed_line = int(m.group(2))
            suffix       = m.group(3)
            # 다음 줄에서 코드 스니펫 추출 (들여쓰기 제거)
            snippet = result_lines[i + 1].strip() if i + 1 < len(result_lines) else ""
            if snippet:
                found = next(
                    (j for j, fl in enumerate(file_lines, 1) if snippet in fl),
                    None
                )
                if found and found != claimed_line:
                    output.append(f"{prefix}:{found}]{suffix}  ※{claimed_line}→{found}")
                    continue
        output.append(line)
    return "\n".join(output)


# read_file 전용 스키마 (리뷰 중 write/bash 호출 차단)
_REVIEW_TOOLS = [
    {"type": "function", "function": {
        "name": "read_file",
        "description": "파일을 읽고 내용을 반환",
        "parameters": {"type": "object", "required": ["path"], "properties": {
            "path":        {"type": "string"},
            "limit_lines": {"type": "integer"},
            "offset":      {"type": "integer"},
        }},
    }},
]

# ── 코드 리뷰 전용 호출 (3-턴 툴 루프) ────────────────────────────────────────
def call_qwen_review(file_path: str, user_instruction: str, session_id: str = "default") -> str:
    """코드 리뷰 전용 — 실제 tool loop 사용.
    Phase 1: tool_choice=auto  → Qwen이 read_file을 직접 호출 (500줄씩)
    Phase 2: tool_choice=none  → 읽기 완료 후 분석만 강제
    prefetch/direct route 완전 우회.
    """
    try:
        total_lines = sum(1 for _ in open(file_path, encoding="utf-8"))
    except Exception as e:
        return f"❌ 파일 확인 실패: {e}"

    needed_reads = (total_lines + 499) // 500  # 500줄씩 몇 번 읽어야 하는지

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content":
            f"{user_instruction}\n\n"
            f"파일: {file_path} (총 {total_lines}줄)\n"
            f"read_file 도구로 offset=0 limit=500부터 시작해서 {needed_reads}번 읽어라 (500줄씩). "
            f"모든 청크를 읽은 후에만 인용 목록을 출력하라. "
            f"도구를 호출하지 않고 직접 답하면 INSUFFICIENT_EVIDENCE만 출력."
        },
    ]

    read_count = 0

    for _round in range(20):
        # 충분히 읽었으면 tools 스키마 없는 분석 전용 호출로 전환
        phase2 = read_count >= needed_reads

        if phase2:
            messages.append({"role": "user", "content":
                f"파일 읽기 완료 ({read_count}번/{needed_reads}번). "
                f"이제 위에서 읽은 내용만 바탕으로 인용 목록을 출력하라. "
                f"[{file_path}:라인번호] 코드스니펫 형태. 판정 금지. 도구 추가 호출 금지."})
            try:
                r = requests.post(
                    LM_STUDIO_URL,
                    headers=LM_STUDIO_HEADERS,
                    json={"model": QWEN_MODEL, "messages": messages,
                          "temperature": 0.2, "max_tokens": 4096},  # tools 없음
                    timeout=(5, 600),
                )
                r.raise_for_status()
                content = (r.json().get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
                content = _verify_citations(content, file_path)
                return content or "INSUFFICIENT_EVIDENCE"
            except Exception as e:
                return f"⚠️ 리뷰 분석 실패: {e}"

        try:
            r = requests.post(
                LM_STUDIO_URL,
                headers=LM_STUDIO_HEADERS,
                json={"model": QWEN_MODEL, "messages": messages,
                      "tools": _REVIEW_TOOLS, "tool_choice": "auto",
                      "temperature": 0.2, "max_tokens": 4096},
                timeout=(5, 600),
            )
            r.raise_for_status()
        except Exception as e:
            return f"⚠️ 리뷰 호출 실패: {e}"

        message    = (r.json().get("choices") or [{}])[0].get("message", {})
        native_tcs = message.get("tool_calls") or []
        content    = (message.get("content") or "").strip()

        if not native_tcs:
            return content or "INSUFFICIENT_EVIDENCE"

        # read_file 실행
        tc      = native_tcs[0]
        func    = tc.get("function", {})
        tc_id   = tc.get("id", f"review_{_round}")
        try:
            args = json.loads(func.get("arguments", "{}"))
        except Exception:
            args = {}

        tool_result = _run_tool({"tool": func.get("name", ""), **args})
        read_count += 1
        logger.info("[review] read_file #%d (offset=%s) → %d chars",
                    read_count, args.get("offset", 0), len(tool_result))

        messages.append({"role": "assistant", "content": content or None,
                         "tool_calls": native_tcs})
        messages.append({"role": "tool", "tool_call_id": tc_id, "content": tool_result})

        # 런타임이 다음 offset을 명시적으로 지시 (Qwen이 스스로 offset 진행 못하는 문제 방지)
        next_offset = read_count * 500
        if next_offset < total_lines:
            messages.append({"role": "user", "content":
                f"읽기 {read_count}/{needed_reads} 완료. "
                f"다음: read_file path={file_path} offset={next_offset} limit=500 으로 읽어라."})

    return "INSUFFICIENT_EVIDENCE"


# ── 텔레그램 ───────────────────────────────────────────────────────────────────
def tg_send(text: str):
    # 줄 단위로 4000자 이하 청크 분할 (표/코드블록 중간 잘림 방지)
    lines = text.splitlines(keepends=True)
    chunks, cur = [], ""
    for line in lines:
        if len(cur) + len(line) > 4000:
            if cur:
                chunks.append(cur)
            cur = line
        else:
            cur += line
    if cur:
        chunks.append(cur)
    if not chunks:
        chunks = [""]
    for chunk in chunks:
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
def _prefetch_files(task_text: str) -> str:
    """태스크 텍스트에 언급된 파일 경로를 미리 읽어 내용을 첨부."""
    import re as _re
    # 절대경로 (/home/ubuntu/...) 감지
    paths = _re.findall(r'/home/ubuntu/[^\s\'"<>]+\.py', task_text)
    # 짧은 파일명 (예: auto_trader.py, kis_client.py) → WORKSPACE 기준으로 해석
    short_names = _re.findall(r'[\w\uAC00-\uD7A3]+\.py', task_text)
    for name in short_names:
        full = os.path.join(WORKSPACE, name)
        if os.path.exists(full) and full not in paths:
            paths.append(full)
        # mock_trading 하위도 검색
        sub = os.path.join(WORKSPACE, "mock_trading", name)
        if os.path.exists(sub) and sub not in paths:
            paths.append(sub)
    # 중복 제거, 최대 2개
    seen = []
    for p in paths:
        if p not in seen:
            seen.append(p)
        if len(seen) >= 2:
            break
    if not seen:
        return task_text, False

    # 태스크에서 함수명 감지 (예: _monitor_signal_shifts, calculate_chart_signals)
    func_names = _re.findall(r'\b([a-z_][a-z0-9_]*\(\))', task_text)
    func_names = [f.rstrip('()') for f in func_names]

    attachments = []
    for path in seen:
        try:
            with open(path, encoding="utf-8") as f:
                lines = f.readlines()
            total = len(lines)

            # 함수명이 언급된 경우 해당 함수 범위만 추출 (토큰 절약)
            chunk = None
            for func in func_names:
                for i, line in enumerate(lines):
                    if f"def {func}" in line:
                        # 함수 시작 ~ 다음 def/class 또는 최대 120줄
                        end = i + 1
                        while end < len(lines) and end < i + 120:
                            if end > i and _re.match(r'^(def |class )', lines[end]):
                                break
                            end += 1
                        chunk = lines[i:end]
                        logger.info("[prefetch] %s → %s() 함수 %d~%d줄 추출",
                                    path, func, i+1, end)
                        break
                if chunk:
                    break

            # 함수 못 찾으면 앞 150줄 (기존 300 → 절반으로)
            if chunk is None:
                chunk = lines[:150]
                suffix = f"\n... (총 {total}줄, 앞 150줄만 표시)" if total > 150 else ""
                logger.info("[prefetch] %s → 앞 150줄 첨부", path)
            else:
                suffix = ""

            content_str = "".join(chunk)
            attachments.append(f"\n\n--- 📄 {path} ({total}줄) ---\n{content_str}{suffix}\n--- EOF ---")
        except Exception as e:
            logger.warning("[prefetch] 파일 읽기 실패 %s: %s", path, e)

    if not attachments:
        return task_text, False

    msg = (task_text
           + "\n\n[파일 내용 첨부 완료 — XML 도구 태그 사용 금지. 위 코드를 바탕으로 바로 분석하세요.]"
           + "".join(attachments))
    return msg, True   # True = 파일 첨부됨 → 도구 루프 불필요


_ANALYSIS_SYSTEM = """너는 서버 보수 협업 조수다. 한국어로 답변한다.
코드 분석 요청 시: 버그/레이스컨디션/개선점을 구체적으로 보고한다.
파일 내용이 [파일 내용 첨부 완료] 태그로 첨부되어 있으면 그 코드를 직접 분석하라.
XML 태그 사용 금지. 순수 텍스트로만 답변."""


def _call_qwen_direct(user_msg: str, session_id: str) -> str:
    """도구 루프 없이 1회 호출 — 파일 이미 첨부된 분석 태스크용.
    CLAUDE.md 없는 경량 시스템 프롬프트 사용 → 입력 토큰 절약.
    """
    messages = [
        {"role": "system", "content": _ANALYSIS_SYSTEM},
        {"role": "user", "content": user_msg},
    ]
    try:
        r = requests.post(
            LM_STUDIO_URL,
            headers=LM_STUDIO_HEADERS,
            json={"model": QWEN_MODEL, "messages": messages,
                  "temperature": 0.2, "max_tokens": 4096, "reasoning_effort": "low", "max_reasoning_tokens": 2000},
            timeout=(5, 600),  # 최대 10분 허용
        )
        r.raise_for_status()
        data2 = r.json()
        # 모델 언로드 감지
        if _is_model_unloaded_resp(data2):
            logger.warning("직접 호출 — 모델 언로드 응답 → WoL 후 모델 로드 대기")
            tg_send("⚠️ LM Studio 모델 언로드 감지 → WoL 전송 중...")
            send_wol()
            if _wait_for_model(180):
                tg_send("✅ 모델 로드 확인 — 재호출")
                r3 = requests.post(
                    LM_STUDIO_URL, headers=LM_STUDIO_HEADERS,
                    json={"model": QWEN_MODEL, "messages": messages,
                          "temperature": 0.2, "max_tokens": 4096},
                    timeout=(5, 600),
                )
                r3.raise_for_status()
                data2 = r3.json()
            else:
                return "⚠️ 모델 로드 실패 (180초 대기)"
        content = (data2.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
        # 혹시 tool 태그가 섞여 있으면 제거
        content = re.sub(r'<(read_file|write_file|replace_text|bash)[^>]*/?>.*?(?:</\1>|(?=\n\n))', '', content, flags=re.DOTALL).strip()
        return content or "⚠️ 응답 없음"
    except Exception as e:
        err_str = str(e).lower()
        if any(k in err_str for k in ["connect", "timeout", "refused", "remotedisconnected"]):
            logger.warning("직접 호출 PC 연결 실패 → WoL 전송 후 1회 재시도")
            tg_send("⚠️ PC 연결 실패 → WoL 전송 중...")
            send_wol()
            if _wait_for_model(180):
                tg_send("✅ PC 응답 확인 — 재호출")
                try:
                    r2 = requests.post(
                        LM_STUDIO_URL,
                        headers=LM_STUDIO_HEADERS,
                        json={"model": QWEN_MODEL, "messages": messages,
                              "temperature": 0.2, "max_tokens": 4096, "reasoning_effort": "low", "max_reasoning_tokens": 2000},
                        timeout=(5, 600),
                    )
                    r2.raise_for_status()
                    content2 = (r2.json().get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
                    content2 = re.sub(r'<(read_file|write_file|replace_text|bash)[^>]*/?>.*?(?:</\1>|(?=\n\n))', '', content2, flags=re.DOTALL).strip()
                    return content2 or "⚠️ 응답 없음"
                except Exception as e2:
                    logger.error("직접 호출 재시도 실패: %s", e2)
                    return f"⚠️ 연결 실패(재시도): {e2}"
            else:
                tg_send("❌ PC 120초 내 응답 없음")
        logger.error("직접 호출 실패: %s", e)
        return f"⚠️ 연결 실패: {e}"


def _route_qwen(text: str, session_id: str) -> str:
    """is_modify/has_code 분기 공통 라우터 — 적절한 Qwen 호출 경로 선택."""
    import re as _re
    # 코드 리뷰 태스크 → call_qwen_review (prefetch/direct route 완전 우회)
    review_match = _re.search(r'코드\s*리뷰|1패스|code\s*review|점검해|전체\s*리뷰', text, re.IGNORECASE)
    file_match   = _re.search(r'/home/ubuntu/[^\s\'\"<>]+\.py|[\w\uAC00-\uD7A3]+\.py', text)
    if review_match and file_match:
        fpath = file_match.group(0)
        if not fpath.startswith("/"):
            fpath = os.path.join(WORKSPACE, fpath)
        if os.path.exists(fpath):
            logger.info("[review route] %s → call_qwen_review", fpath)
            return call_qwen_review(fpath, text, session_id)

    is_modify = bool(_re.search(r'(수정|변경|추가|삭제|삽입)(해|줘|해줘|하세요)|바꿔|고쳐|write_file|replace_text', text))
    has_code  = bool(re.search(r'```[\s\S]+?```', text))  # 실제 코드블록 쌍 존재 여부
    if not is_modify and not has_code:
        enriched, prefetched = _prefetch_files(text)
        if prefetched:
            return _call_qwen_direct(enriched, session_id)
        return call_qwen(text, session_id=session_id)
    elif has_code and not is_modify:
        return _call_qwen_direct(text, session_id)
    else:
        return call_qwen(text, session_id=session_id)


# ── 태스크 결과 저장 (Claude-Qwen 1:1 협업 채널) ─────────────────────────────
_task_results: list = []          # 최근 결과 저장 (최대 10개)
_task_results_lock = threading.Lock()


def _store_result(task_id: str, task_text: str, reply: str):
    with _task_results_lock:
        _task_results.append({
            "id": task_id,
            "task": task_text[:200],
            "result": reply,
            "timestamp": time.time(),
            "done": True,
        })
        if len(_task_results) > 10:
            _task_results.pop(0)


def _process_task(task_text: str, task_id: str = ""):
    """백그라운드에서 Qwen 호출 후 텔레그램 + 결과 저장."""
    import uuid
    if not task_id:
        task_id = "task_" + uuid.uuid4().hex[:8]
    session_id = task_id
    logger.info("[Claude→Qwen] 작업 수신 [%s]: %s", task_id, task_text[:80])
    tg_send(f"📋 Claude 작업지시 수신:\n{task_text}\n\n⏳ 처리 중...")
    try:
        requests.post("http://127.0.0.1:11435/touch_timer", timeout=2)
    except Exception:
        logger.debug("touch_timer 스킵 (proxy 미실행 — 무시)")
    _stop_keepalive = threading.Event()
    _start_task_keepalive(_stop_keepalive)
    try:
        try:
            from graphify_wrapper import inject_graph_context as _igc
            task_text = _igc(task_text)
        except Exception:
            pass
        reply = _route_qwen(task_text, session_id)
    finally:
        _stop_keepalive.set()
    _store_result(task_id, task_text, reply)
    tg_send(f"✅ 작업 완료:\n{reply}")
    logger.info("[Claude→Qwen] 작업 완료 [%s]: %d chars", task_id, len(reply))


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
            import uuid as _uuid
            task_id = "task_" + _uuid.uuid4().hex[:8]
            threading.Thread(target=_process_task, args=(task, task_id), daemon=True).start()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "accepted", "task_id": task_id, "task": task[:100]}).encode())
        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def do_GET(self):
        """Claude가 Qwen 결과를 직접 조회하는 엔드포인트."""
        try:
            if self.path == "/result":
                # 최신 결과 1개
                with _task_results_lock:
                    result = _task_results[-1] if _task_results else None
                self.send_response(200 if result else 204)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(result or {}).encode())
            elif self.path == "/results":
                # 전체 결과 목록
                with _task_results_lock:
                    results = list(_task_results)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(results).encode())
            elif self.path.startswith("/result/"):
                # 특정 task_id 조회: /result/task_abc12345
                target_id = self.path[len("/result/"):]
                with _task_results_lock:
                    found = next((r for r in _task_results if r["id"] == target_id), None)
                self.send_response(200 if found else 404)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(found or {"error": "not found"}).encode())
            elif self.path.startswith("/wait/"):
                # Long-polling: 태스크 완료까지 블로킹 대기
                # /wait/task_abc12345 또는 /wait/task_abc12345?timeout=600
                from urllib.parse import urlparse, parse_qs
                parsed = urlparse(self.path)
                target_id = parsed.path[len("/wait/"):]
                qs = parse_qs(parsed.query)
                timeout = int(qs.get("timeout", ["600"])[0])
                deadline = time.time() + timeout
                found = None
                while time.time() < deadline:
                    with _task_results_lock:
                        found = next((r for r in _task_results if r["id"] == target_id), None)
                    if found:
                        break
                    time.sleep(2)
                self.send_response(200 if found else 408)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(found or {"status": "timeout", "task_id": target_id}).encode())
            else:
                self.send_response(404)
                self.end_headers()
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
def _start_task_keepalive(stop_event: threading.Event):
    """태스크 처리 중에만 270초마다 /ping_sleep_timer 호출 → 작업 중 절전 방지."""
    def _loop():
        while not stop_event.wait(270):
            try:
                requests.get("http://localhost:11435/ping_sleep_timer", timeout=5)
                logger.info("[킵얼라이브] 작업 중 슬립 타이머 리셋")
            except Exception:
                pass
    threading.Thread(target=_loop, daemon=True).start()


def main():
    # HTTP 태스크 서버 백그라운드 시작
    threading.Thread(target=start_task_server, daemon=True).start()

    logger.info("서버보수에이전트 시작 (Qwen → worker 봇)")
    tg_send("🔧 서버보수에이전트 시작됨\nQwen3.5-27B 연결 완료.\n\n• 텔레그램으로 직접 대화 가능\n• Claude 자동 작업지시: localhost:8001/task")
    offset = 0
    _poll_fail = 0
    while True:
        updates, offset = tg_poll(offset)
        if not updates and offset == offset:
            _poll_fail += 1
            if _poll_fail >= 30:
                logger.warning("tg_poll 연속 30회 빈 응답 — 네트워크 이상 의심")
                _poll_fail = 0
        else:
            _poll_fail = 0
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
            # 텔레그램 대화별 세션 UUID (히스토리 오염 방지)
            if from_id not in _tg_sessions:
                import uuid as _uuid
                _tg_sessions[from_id] = "tg_" + _uuid.uuid4().hex[:8]
            session_id = _tg_sessions[from_id]
            reply = _route_qwen(text, session_id)
            tg_send(reply)
            logger.info("응답 완료: %d chars", len(reply))


if __name__ == "__main__":
    main()
