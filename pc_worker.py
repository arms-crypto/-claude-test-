#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pc_worker.py — Mistral 기반 PC 작업자

사용법:
  python3 pc_worker.py "작업 내용을 여기에"
  python3 pc_worker.py --task "코드 점검해줘" --files auto_trader.py llm_client.py

Mistral(221.144.111.116:11434)이 파일 읽기/쓰기/bash 실행 도구로
코드 작업을 수행하고 결과를 텔레그램으로 보고합니다.
"""

import sys, os, json, time, subprocess, argparse, requests, textwrap
sys.path.insert(0, "/home/ubuntu/-claude-test-")
os.chdir("/home/ubuntu/-claude-test-")

# ── 설정 ─────────────────────────────────────────────────────────────────────
OLLAMA_URL  = "http://221.144.111.116:11434/api/chat"
MODEL       = "mistral-small3.1:24b"
TG_TOKEN    = "8707168013:AAH5yIsoaLoxcA0Lthiw7RaIzD1YcJx8cc8"
CHAT_ID     = "8448138406"
NO_PROXY    = {"http": None, "https": None}
BASE_DIR    = "/home/ubuntu/-claude-test-"
MAX_ITER    = 10   # 최대 도구 호출 반복 횟수

# ── 텔레그램 전송 ─────────────────────────────────────────────────────────────
def tg_send(text: str):
    base = f"https://api.telegram.org/bot{TG_TOKEN}"
    # 4096자 초과 시 분할
    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
    for chunk in chunks:
        try:
            requests.post(f"{base}/sendMessage",
                          json={"chat_id": CHAT_ID, "text": chunk},
                          proxies=NO_PROXY, timeout=10)
        except Exception as e:
            print(f"[TG 전송 실패] {e}")

# ── 도구 정의 ─────────────────────────────────────────────────────────────────
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "파일 내용을 읽어 반환합니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "읽을 파일 경로 (절대 또는 프로젝트 기준 상대경로)"},
                    "offset": {"type": "integer", "description": "시작 줄 번호 (기본값 1)"},
                    "limit":  {"type": "integer", "description": "읽을 줄 수 (기본값 200)"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "파일 전체를 덮어씁니다. 작은 수정은 edit_file을 사용하세요.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path":    {"type": "string", "description": "쓸 파일 경로"},
                    "content": {"type": "string", "description": "파일 전체 내용"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "파일에서 old_str을 new_str로 정확히 1회 치환합니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path":    {"type": "string", "description": "수정할 파일 경로"},
                    "old_str": {"type": "string", "description": "대체될 기존 문자열 (파일 내 유일해야 함)"},
                    "new_str": {"type": "string", "description": "대체할 새 문자열"},
                },
                "required": ["path", "old_str", "new_str"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "쉘 명령어를 실행하고 stdout/stderr를 반환합니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "실행할 bash 명령어"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "report",
            "description": "작업 결과를 텔레그램으로 보고하고 작업을 종료합니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "텔레그램으로 보낼 보고 내용"},
                },
                "required": ["message"],
            },
        },
    },
]

# ── 도구 실행 ─────────────────────────────────────────────────────────────────
def _resolve_path(path: str) -> str:
    if not os.path.isabs(path):
        return os.path.join(BASE_DIR, path)
    return path

def execute_tool(name: str, args: dict) -> str:
    try:
        if name == "read_file":
            path = _resolve_path(args["path"])
            offset = max(0, args.get("offset", 1) - 1)
            limit  = args.get("limit", 200)
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            selected = lines[offset:offset + limit]
            result = "".join(f"{offset+i+1}: {l}" for i, l in enumerate(selected))
            return result or "(빈 파일)"

        elif name == "write_file":
            path = _resolve_path(args["path"])
            with open(path, "w", encoding="utf-8") as f:
                f.write(args["content"])
            return f"✅ {path} 저장 완료 ({len(args['content'])}자)"

        elif name == "edit_file":
            path = _resolve_path(args["path"])
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            old = args["old_str"]
            new = args["new_str"]
            if old not in content:
                return f"❌ old_str을 파일에서 찾을 수 없음: {repr(old[:80])}"
            count = content.count(old)
            if count > 1:
                return f"❌ old_str이 {count}곳에 존재 — 더 구체적인 문자열 사용 필요"
            with open(path, "w", encoding="utf-8") as f:
                f.write(content.replace(old, new, 1))
            return f"✅ {path} 수정 완료"

        elif name == "bash":
            result = subprocess.run(
                args["command"], shell=True, capture_output=True,
                text=True, timeout=60, cwd=BASE_DIR
            )
            out = (result.stdout + result.stderr).strip()
            return out[:3000] if out else "(출력 없음)"

        elif name == "report":
            msg = args["message"]
            tg_send(f"🤖 [PC 작업자 보고]\n\n{msg}")
            # Claude Code에서 읽을 수 있도록 파일로도 저장
            report_path = "/tmp/pc_worker_last_report.txt"
            with open(report_path, "w", encoding="utf-8") as f:
                import datetime
                f.write(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]\n\n{msg}\n")
            return "DONE"

    except Exception as e:
        return f"❌ 오류: {e}"

# ── Mistral 호출 루프 ─────────────────────────────────────────────────────────
SYSTEM_PROMPT = """\
너는 Python 자동매매 시스템의 코드 작업 전담 에이전트다.
주어진 작업을 도구(read_file, edit_file, bash 등)를 사용해 직접 수행하고,
완료 후 반드시 report 도구로 결과를 보고해야 한다.

규칙:
- pre-injection 블록(ai_chat.py 3-1/3-2/3-3) 절대 제거 금지
- proxy_v54.py 단일 파일로 되돌리기 금지
- RSI 기준 50 유지 (30~70으로 바꾸지 말 것)
- buy_count 12신호 유지 (분봉 포함 금지)
- 일목균형표 파라미터 HTS 설정 유지 (전환1/기준1/선행2)
- 확실하지 않으면 수정하지 말고 report로 판단 보고
"""

def run_worker(task: str, extra_files: list = None):
    print(f"[PC 작업자] 작업 시작: {task[:80]}")
    tg_send(f"🔧 [PC 작업자 시작]\n\n{task[:200]}")

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user",   "content": task},
    ]

    # 초기 파일 컨텍스트 주입
    if extra_files:
        for fpath in extra_files:
            full = _resolve_path(fpath)
            try:
                with open(full, "r", encoding="utf-8") as f:
                    content = f.read()
                lines = content.splitlines()
                preview = "\n".join(f"{i+1}: {l}" for i, l in enumerate(lines[:150]))
                messages.append({
                    "role": "user",
                    "content": f"[파일: {fpath}]\n{preview}\n{'...(이하 생략)' if len(lines)>150 else ''}"
                })
            except Exception as e:
                messages.append({"role": "user", "content": f"[파일 읽기 실패: {fpath}] {e}"})

    for iteration in range(MAX_ITER):
        print(f"[PC 작업자] Mistral 호출 #{iteration+1}")
        try:
            resp = requests.post(
                OLLAMA_URL,
                json={
                    "model":    MODEL,
                    "messages": messages,
                    "tools":    TOOLS,
                    "stream":   False,
                },
                timeout=120,
                proxies=NO_PROXY,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            err = f"❌ Mistral 호출 실패: {e}"
            print(err)
            tg_send(f"🤖 [PC 작업자 오류]\n\n{err}")
            return

        msg = data.get("message", {})
        tool_calls = msg.get("tool_calls", [])
        content    = msg.get("content", "")

        # 도구 호출 없으면 종료
        if not tool_calls:
            print(f"[PC 작업자] 도구 없음, 종료: {content[:100]}")
            if content.strip():
                tg_send(f"🤖 [PC 작업자 완료]\n\n{content}")
            return

        # assistant 메시지 추가
        messages.append({"role": "assistant", "content": content, "tool_calls": tool_calls})

        # 각 도구 실행
        done = False
        for tc in tool_calls:
            fn   = tc.get("function", {})
            name = fn.get("name", "")
            raw  = fn.get("arguments", {})
            args = raw if isinstance(raw, dict) else json.loads(raw)

            print(f"  → {name}({list(args.keys())})")
            result = execute_tool(name, args)

            messages.append({
                "role":         "tool",
                "content":      result,
                "tool_call_id": tc.get("id", ""),
            })

            if name == "report" and result == "DONE":
                done = True
                break

        if done:
            print("[PC 작업자] report 완료, 종료")
            return

    tg_send("🤖 [PC 작업자]\n\n⚠️ 최대 반복 횟수 초과 — 작업 미완료")
    print("[PC 작업자] 최대 반복 초과")


# ── 진입점 ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="PC 작업자 — Mistral 코드 에이전트")
    parser.add_argument("task", nargs="?", help="작업 내용")
    parser.add_argument("--task", "-t", dest="task_opt", help="작업 내용 (플래그)")
    parser.add_argument("--files", "-f", nargs="*", help="미리 읽어줄 파일 목록")
    args = parser.parse_args()

    task = args.task or args.task_opt
    if not task:
        print("사용법: python3 pc_worker.py '작업 내용'")
        print("예시:   python3 pc_worker.py '아래 3가지 버그를 수정해줘...'")
        sys.exit(1)

    run_worker(task, extra_files=args.files or [])
