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
QWEN_MODEL    = "qwen3.5-27b-claude-4.6-opus-reasoning-distilled-heretic-v2-i1"
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

### 2-A. 단순 텍스트 (따옴표 없을 때)
<replace_text path="/home/ubuntu/-claude-test-/파일경로" old="바꿀 원본 텍스트" new="새 텍스트"/>

### 2-B. ⚠️ old/new 내용에 따옴표(", ')가 포함될 때 — 반드시 태그 형식 사용
<replace_text path="/home/ubuntu/-claude-test-/파일경로"><old>
바꿀 원본 텍스트 (따옴표 "포함" 가능)
</old><new>
새 텍스트 ("따옴표" 자유롭게 사용)
</new></replace_text>

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

    # replace_text: <replace_text path="..." old="..." new="..."/>  ← 핵심 도구
    # 1단계: path 속성만 추출
    m_path = re.search(r'<replace_text\s+path=["\']([^"\']+)["\']', content)
    if m_path:
        path_val = m_path.group(1)
        tag_start = m_path.start()
        # 2단계: old= 와 new= 값을 re.DOTALL + [\s\S]*? 로 추출 (이스케이프/줄바꿈 포함)
        m_old_new = re.search(
            r'old=["\'](\\.+?|[\s\S]*?)["\'][\s\S]*?new=["\'](\\.+?|[\s\S]*?)["\']',
            content[tag_start:], re.DOTALL
        )
        if m_old_new:
            return {"tool": "replace_text", "path": path_val,
                    "old": m_old_new.group(1), "new": m_old_new.group(2)}
    # 멀티라인 old/new 태그 형식 지원
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
                return f"[앞 200줄만 표시 (총 {total}줄) — 더 보려면 limit_lines/offset 사용]\n" + "".join(lines[:200])
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
    for _round in range(15):
        try:
            r = requests.post(
                LM_STUDIO_URL,
                json={"model": QWEN_MODEL, "messages": messages,
                      "temperature": 0.3, "max_tokens": 4096},
                timeout=(5, 600),
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

            # 같은 도구+경로 중복 실행 차단 (루프 방지)
            tool_key = f"{tool_call.get('tool')}:{tool_call.get('path','')}"
            if tool_key in _executed and tool_call.get("tool") == "read_file":
                # 중복 read_file → 분석 강제
                logger.warning("중복 read_file 감지 (%s) — 분석 강제 전환", tool_key)
                messages.append({"role": "assistant", "content": content})
                messages.append({"role": "user",
                                 "content": "같은 파일을 또 읽으려 하고 있습니다. 이미 파일 내용을 받았습니다. XML 태그 없이 순수 텍스트로 분석 결과만 보고하세요."})
                continue
            _executed.add(tool_key)

            tool_result = _run_tool(tool_call)
            tool_name = tool_call.get("tool")
            logger.info("도구 실행: %s → %d chars", tool_name, len(tool_result))

            # 도구별 맞춤 피드백
            # 수정 요청 감지: 명령형 패턴만 (예: "수정해", "변경해줘", "바꿔", "고쳐")
            # "수정 없이", "수정하지 말고" 같은 부정 패턴 제외
            import re as _re2
            is_modify_task = bool(_re2.search(r'(수정|변경)(해|줘|해줘|하세요)|바꿔|고쳐|replace|write_file', user_msg))
            if tool_name == "read_file":
                if is_modify_task:
                    next_step = "파일 내용 확인 완료. 이제 <replace_text> 또는 <write_file>로 수정을 진행하세요. 더 이상 read_file을 반복하지 마세요."
                else:
                    next_step = "파일 내용 확인 완료. 요청한 분석을 한국어 텍스트로만 보고하세요. XML 태그(<read_file>, <write_file> 등) 절대 사용 금지. 순수 텍스트로만 답변하세요."
            elif tool_name == "write_file":
                next_step = "파일 저장 완료. 변경 내용을 한국어로 보고하세요."
            elif tool_name == "replace_text":
                next_step = "교체 완료. 다음 수정이 있으면 진행하고, 없으면 완료 보고를 하세요."
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
            json={"model": QWEN_MODEL, "messages": messages,
                  "temperature": 0.3, "max_tokens": 4096},
            timeout=(5, 600),  # 최대 10분 허용
        )
        r.raise_for_status()
        content = (r.json().get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
        # 혹시 tool 태그가 섞여 있으면 제거
        content = re.sub(r'<(read_file|write_file|replace_text|bash)[^>]*/?>.*?(?:</\1>|(?=\n\n))', '', content, flags=re.DOTALL).strip()
        return content or "⚠️ 응답 없음"
    except Exception as e:
        logger.error("직접 호출 실패: %s", e)
        return f"⚠️ 연결 실패: {e}"


def _route_qwen(text: str, session_id: str) -> str:
    """is_modify/has_code 분기 공통 라우터 — 적절한 Qwen 호출 경로 선택."""
    import re as _re
    is_modify = bool(_re.search(r'(수정|변경)(해|줘|해줘|하세요)|바꿔|고쳐', text))
    has_code  = '```' in text or len(text) > 2000
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
    tg_send(f"📋 Claude 작업지시 수신:\n{task_text[:200]}\n\n⏳ 처리 중...")
    reply = _route_qwen(task_text, session_id)
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
