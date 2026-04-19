#!/usr/bin/env python3
"""graphify.py — Graphify CLI (build / watch / hook / claude / claw install)."""
import argparse
import json
import pathlib
import re
import sys
import time

ROOT = pathlib.Path("/home/ubuntu/-claude-test-")
GRAPH_JSON = ROOT / "graphify-out" / "graph.json"
GITIGNORE = ROOT / ".gitignore"
CLAUDE_MD = ROOT / "CLAUDE.md"
GIT_HOOK = ROOT / ".git" / "hooks" / "post-commit"
TASK_SERVER = "http://127.0.0.1:8001"


# ── helpers ──────────────────────────────────────────────────────────────────

def _log(msg: str, quiet: bool = False):
    if not quiet:
        print(msg)


def _ensure_gitignore():
    entry = "graphify-out/"
    if GITIGNORE.exists():
        content = GITIGNORE.read_text()
        if entry in content:
            return
        GITIGNORE.write_text(content.rstrip() + "\n" + entry + "\n")
    else:
        GITIGNORE.write_text(entry + "\n")


# ── cmd: build ───────────────────────────────────────────────────────────────

def cmd_build(args):
    from graphify_core import run_build
    root = pathlib.Path(args.root).resolve()
    _log(f"[graphify] building graph for {root} ...", args.quiet)
    g = run_build(root)
    _ensure_gitignore()
    _log(
        f"[graphify] done: {g['meta']['total_symbols']} symbols, "
        f"{g['meta']['python_files']} files",
        args.quiet,
    )
    _log(f"  → {GRAPH_JSON}", args.quiet)
    _log(f"  → {ROOT / 'graphify-out' / 'GRAPH_REPORT.md'}", args.quiet)


# ── cmd: watch ───────────────────────────────────────────────────────────────

def cmd_watch(args):
    from graphify_core import is_stale, run_build
    root = pathlib.Path(args.root).resolve()
    _log(f"[graphify] watching {root} every {args.interval}s ...", args.quiet)

    try:
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler

        class _Handler(FileSystemEventHandler):
            def __init__(self):
                self._dirty = False

            def on_any_event(self, event):
                if event.src_path.endswith(".py"):
                    self._dirty = True

        handler = _Handler()
        observer = Observer()
        observer.schedule(handler, str(root), recursive=True)
        observer.start()
        _log("[graphify] watchdog active", args.quiet)
        try:
            while True:
                time.sleep(args.interval)
                if handler._dirty:
                    handler._dirty = False
                    _log("[graphify] change detected, rebuilding ...", args.quiet)
                    run_build(root)
                    _log("[graphify] rebuild done", args.quiet)
        finally:
            observer.stop()
            observer.join()
    except ImportError:
        _log("[graphify] watchdog not installed, using polling", args.quiet)
        while True:
            time.sleep(args.interval)
            if is_stale(root):
                _log("[graphify] stale, rebuilding ...", args.quiet)
                run_build(root)
                _log("[graphify] rebuild done", args.quiet)


# ── cmd: hook install ─────────────────────────────────────────────────────────

def cmd_hook_install(args):
    script = (
        "#!/bin/sh\n"
        f"python3 {ROOT / 'graphify.py'} . --quiet 2>/dev/null &\n"
    )
    if GIT_HOOK.exists() and GIT_HOOK.read_text() == script:
        _log("[graphify] post-commit hook already installed")
        return
    GIT_HOOK.parent.mkdir(parents=True, exist_ok=True)
    GIT_HOOK.write_text(script)
    GIT_HOOK.chmod(0o755)
    _log(f"[graphify] installed post-commit hook → {GIT_HOOK}")


def cmd_hook_uninstall(args):
    if GIT_HOOK.exists():
        GIT_HOOK.unlink()
        _log("[graphify] removed post-commit hook")
    else:
        _log("[graphify] no hook to remove")


# ── cmd: claude install ───────────────────────────────────────────────────────

_CLAUDE_SECTION = """
## Code Graph (Graphify)
- 코드베이스 그래프: `graphify-out/GRAPH_REPORT.md` 참고 (300줄 이내 요약)
- 상세 쿼리: `graphify-out/graph.json` (symbol → file/line/calls)
- 파일별 상세: `graphify-out/doc/`
- 재생성: `python3 graphify.py .`
"""


def cmd_claude_install(args):
    if not CLAUDE_MD.exists():
        _log(f"[graphify] CLAUDE.md not found at {CLAUDE_MD}")
        return
    content = CLAUDE_MD.read_text(encoding="utf-8")
    if "Code Graph (Graphify)" in content:
        _log("[graphify] CLAUDE.md already has Code Graph section")
        return
    CLAUDE_MD.write_text(content.rstrip() + "\n" + _CLAUDE_SECTION)
    _log(f"[graphify] injected Code Graph section into {CLAUDE_MD}")


# ── cmd: claw install ─────────────────────────────────────────────────────────

_PATCH_TASK = (
    "서버보수에이전트.py 파일을 read_file로 읽고, "
    "_process_task 함수 안에서 reply = _route_qwen(task_text, session_id) 호출 직전에 "
    "다음 4줄을 replace_text로 삽입해줘 (코드블록 없이 plain text로 전달함): "
    "        try:\n"
    "            from graphify_wrapper import inject_graph_context as _igc\n"
    "            task_text = _igc(task_text)\n"
    "        except Exception:\n"
    "            pass\n"
    "삽입 후 grep -n inject_graph_context 서버보수에이전트.py 로 확인 보고해줘."
)


def cmd_claw_install(args):
    import urllib.request
    import urllib.error

    payload = json.dumps({"task": _PATCH_TASK}).encode()
    req = urllib.request.Request(
        f"{TASK_SERVER}/task",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read())
        task_id = body.get("task_id", "")
        _log(f"[graphify] task sent: {task_id}")
    except urllib.error.URLError as e:
        _log(f"[graphify] 태스크 서버(8001)에 연결 실패: {e}")
        _log("  → python3 서버보수에이전트.py 를 먼저 실행하세요")
        return

    if not task_id:
        _log("[graphify] task_id 없음 — 수동으로 서버보수에이전트.py를 패치하세요")
        return

    # 완료 대기
    _log("[graphify] 완료 대기 중 ...")
    wait_url = f"{TASK_SERVER}/wait/{task_id}?timeout=300"
    try:
        with urllib.request.urlopen(wait_url, timeout=310) as resp:
            result = json.loads(resp.read())
        _log("[graphify] Qwen 패치 결과:")
        _log(str(result.get("reply", result))[:500])
    except Exception as e:
        _log(f"[graphify] 대기 중 오류: {e}")


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    argv = sys.argv[1:]

    # graphify hook install/uninstall
    if len(argv) >= 2 and argv[0] == "hook":
        class _A:
            quiet = False
        if argv[1] == "install":
            cmd_hook_install(_A())
        else:
            cmd_hook_uninstall(_A())
        return

    # graphify claude install
    if len(argv) >= 2 and argv[0] == "claude" and argv[1] == "install":
        class _A:
            quiet = False
        cmd_claude_install(_A())
        return

    # graphify claw install
    if len(argv) >= 2 and argv[0] == "claw" and argv[1] == "install":
        class _A:
            quiet = False
        cmd_claw_install(_A())
        return

    parser = argparse.ArgumentParser(
        prog="graphify",
        description="Python AST 코드 그래프 생성기",
    )
    parser.add_argument("root", nargs="?", default=".", help="프로젝트 루트")
    parser.add_argument("--watch", action="store_true", help="파일 변경 감시 후 자동 재빌드")
    parser.add_argument("--interval", type=int, default=30, help="폴링 간격(초)")
    parser.add_argument("--quiet", action="store_true", help="stdout 억제 (git hook용)")

    args = parser.parse_args(argv)

    if args.watch:
        cmd_watch(args)
    else:
        cmd_build(args)


if __name__ == "__main__":
    main()
