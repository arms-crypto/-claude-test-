"""graphify_bench.py — Graphify alias + Qwen 탐색 비용 벤치마크."""
import json
import re
import time
import urllib.request
from graphify_wrapper import inject_graph_context, strip_graph_context

TASK_SERVER = "http://127.0.0.1:8001"

# ── 테스트 케이스 ──────────────────────────────────────────────────────────────
# (label, task_text, expect_graph: True/False)
TEST_CASES = [
    # aliases 매칭 기대
    ("자동매매",    "자동매매 루프에서 매수 슬롯이 몇 개인지 알려줘",               True),
    ("봇1",         "봇1 핸들러에서 메시지 처리 흐름을 설명해줘",                  True),
    ("절전",        "절전 조건이 언제 발동되는지 코드 기준으로 알려줘",             True),
    ("태스크서버",  "태스크서버에서 task_id는 어떻게 생성되는지 알려줘",           True),
    ("장중판단",    "장중판단 함수가 NXT 시간을 어떻게 구분하는지 알려줘",         True),
    ("주가조회",    "주가조회 함수의 파라미터를 알려줘",                            True),
    ("히스토리",    "히스토리 세션 관리 방식을 설명해줘",                           True),
    # 심볼명 직접 언급
    ("symbol직접",  "auto_trade_cycle 함수의 호출 흐름을 설명해줘",               True),
    # 매칭 안 됨 기대
    ("무관키워드",  "오늘 날씨 어때요",                                             False),
    ("영어무관",    "what is the meaning of life",                                  False),
]


def _send_task(task_text: str) -> tuple[str, float]:
    """태스크 전송 후 결과 반환. (reply, elapsed_sec)"""
    payload = json.dumps({"task": task_text}).encode()
    req = urllib.request.Request(
        f"{TASK_SERVER}/task",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        body = json.loads(r.read())
    task_id = body["task_id"]

    t0 = time.time()
    with urllib.request.urlopen(
        f"{TASK_SERVER}/wait/{task_id}?timeout=300", timeout=310
    ) as r:
        result = json.loads(r.read())
    elapsed = time.time() - t0
    return result.get("result", ""), elapsed


def _count_tool_calls(reply: str) -> dict:
    """Qwen 응답에서 도구 호출 횟수 추정 (로그 기반 아님 — 응답 텍스트 분석)."""
    # Qwen은 도구 실행 후 결과를 응답에 반영. 로그에서 실제 카운트가 더 정확하지만
    # 여기서는 reply 텍스트 + task-server.log 패턴으로 근사치 계산
    counts = {
        "read_file": len(re.findall(r'read_file|파일.*읽', reply, re.IGNORECASE)),
        "bash_grep": len(re.findall(r'grep|bash', reply, re.IGNORECASE)),
    }
    return counts


def _parse_log_tool_calls(task_id: str, log_path: str = "task-server.log") -> dict:
    """task-server.log에서 해당 task_id 구간의 tool call 횟수 집계."""
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return {"read_file": 0, "bash": 0, "replace_text": 0, "write_file": 0}

    # task_id 구간 찾기
    in_task = False
    counts = {"read_file": 0, "bash": 0, "replace_text": 0, "write_file": 0}
    for line in lines:
        if task_id in line and "작업 수신" in line:
            in_task = True
        if in_task and "작업 완료" in line and task_id in line:
            break
        if in_task and "native tool_call:" in line:
            for tool in counts:
                if tool in line:
                    counts[tool] += 1
    return counts


def run_bench(run_qwen: bool = True):
    print("=" * 70)
    print("Graphify Alias + Qwen 탐색 비용 벤치마크")
    print("=" * 70)

    results = []

    for label, task, expect_ctx in TEST_CASES:
        print(f"\n[{label}] {task[:45]}...")

        # 1. Graph context 주입 여부 (정적 — 즉시)
        enriched = inject_graph_context(task)
        has_ctx = enriched.startswith("[GRAPH CONTEXT]")
        ctx_nodes = len(re.findall(r'^- \w', enriched, re.MULTILINE)) if has_ctx else 0

        print(f"  GRAPH CONTEXT: {'✅ ' + str(ctx_nodes) + '개 노드' if has_ctx else '❌ 없음'}")

        row = {
            "label": label,
            "task_short": task[:30],
            "expect_ctx": expect_ctx,
            "has_ctx": has_ctx,
            "ctx_nodes": ctx_nodes,
            "read_file": "-",
            "bash_grep": "-",
            "elapsed_sec": "-",
            "ok": "✅" if has_ctx == expect_ctx else "❌",
        }

        # 2. Qwen 실제 호출 (선택)
        if run_qwen:
            try:
                reply, elapsed = _send_task(enriched if has_ctx else task)
                log_counts = _parse_log_tool_calls("", "task-server.log")
                txt_counts = _count_tool_calls(reply)
                row["read_file"] = txt_counts["read_file"]
                row["bash_grep"] = txt_counts["bash_grep"]
                row["elapsed_sec"] = f"{elapsed:.0f}s"
                print(f"  read_file≈{row['read_file']}  bash≈{row['bash_grep']}  {elapsed:.0f}s")
            except Exception as e:
                print(f"  Qwen 호출 실패: {e}")

        results.append(row)

    # ── 결과 테이블 출력 ──────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("결과 테이블")
    print("=" * 70)
    print(f"{'케이스':<12} {'GRAPH':<6} {'노드':<4} {'read':<5} {'bash':<5} {'시간':<6} {'기대일치'}")
    print("-" * 55)
    for r in results:
        ctx_str = "✅" if r["has_ctx"] else "❌"
        print(f"{r['label']:<12} {ctx_str:<6} {r['ctx_nodes']:<4} "
              f"{str(r['read_file']):<5} {str(r['bash_grep']):<5} "
              f"{str(r['elapsed_sec']):<6} {r['ok']}")

    # ── 요약 통계 ─────────────────────────────────────────────────────────────
    total = len(results)
    ctx_hit = sum(1 for r in results if r["has_ctx"])
    expect_match = sum(1 for r in results if r["ok"] == "✅")

    print("\n" + "=" * 70)
    print("요약")
    print("=" * 70)
    print(f"GRAPH CONTEXT 주입률: {ctx_hit}/{total} ({ctx_hit/total*100:.0f}%)")
    print(f"기대 결과 일치율:     {expect_match}/{total} ({expect_match/total*100:.0f}%)")

    if run_qwen:
        read_vals = [r["read_file"] for r in results if r["read_file"] != "-"]
        bash_vals = [r["bash_grep"] for r in results if r["bash_grep"] != "-"]
        ctx_read  = [r["read_file"] for r in results if r["has_ctx"] and r["read_file"] != "-"]
        noctx_read= [r["read_file"] for r in results if not r["has_ctx"] and r["read_file"] != "-"]
        if ctx_read and noctx_read:
            print(f"평균 read_file (ctx 있음): {sum(ctx_read)/len(ctx_read):.1f}")
            print(f"평균 read_file (ctx 없음): {sum(noctx_read)/len(noctx_read):.1f}")
            diff = sum(noctx_read)/len(noctx_read) - sum(ctx_read)/len(ctx_read)
            print(f"탐색 비용 절감:           ≈{diff:.1f} read_file/태스크")

    print("\n운영 지침:")
    print("  - aliases 매칭 시: GRAPH CONTEXT로 파일:라인 즉시 전달 → read_file 절감")
    print("  - aliases 미매칭 시: Qwen이 grep/read_file로 직접 탐색 → 태스크 지연")
    print("  - 권장: 태스크 작성 시 한국어 aliases 또는 symbol명 포함 (CLAUDE.md 규칙)")
    print("  - 비정규장(~20:00) 이후 긴 수정 태스크 전송")


if __name__ == "__main__":
    import sys
    # python3 graphify_bench.py --no-qwen  → 정적 테스트만 (빠름)
    # python3 graphify_bench.py            → Qwen 실제 호출 포함
    run_qwen = "--no-qwen" not in sys.argv
    run_bench(run_qwen=run_qwen)
