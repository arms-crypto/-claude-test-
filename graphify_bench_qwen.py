"""Qwen 실제 호출 — 탐색 비용(read_file/bash 횟수) 측정."""
import json, re, time, urllib.request
from graphify_wrapper import inject_graph_context

TASK_SERVER = "http://127.0.0.1:8001"

# ctx 있음 3개 + 없음 2개 (분석 태스크만 — 파일 수정 없음)
CASES = [
    ("CTX_자동매매",  "자동매매 루프에서 매수 슬롯 최대값이 어디서 결정되는지 read_file로 확인해서 알려줘",        True),
    ("CTX_절전",      "절전 조건이 언제 발동되는지 서버보수에이전트.py를 read_file로 확인해서 알려줘",             True),
    ("CTX_장중판단",  "장중판단 함수 is_trading_hours 로직을 read_file로 확인해서 설명해줘",                       True),
    ("NOCTX_raw1",    "거래 시간 판단 로직이 어느 파일 몇 번째 줄에 있는지 grep으로 찾아서 알려줘",               False),
    ("NOCTX_raw2",    "LM Studio 슬립 타이머 리셋 함수가 어디 있는지 grep으로 찾아서 알려줘",                     False),
]

def send_and_wait(task_text):
    payload = json.dumps({"task": task_text}).encode()
    req = urllib.request.Request(f"{TASK_SERVER}/task", data=payload,
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        body = json.loads(r.read())
    task_id = body["task_id"]
    t0 = time.time()
    with urllib.request.urlopen(f"{TASK_SERVER}/wait/{task_id}?timeout=300", timeout=310) as r:
        result = json.loads(r.read())
    return result.get("result",""), time.time()-t0, task_id

def count_from_log(task_id, log="task-server.log"):
    counts = {"read_file":0, "bash":0, "replace_text":0}
    try:
        lines = open(log, encoding="utf-8", errors="replace").readlines()
    except:
        return counts
    in_task = False
    for line in lines:
        if task_id in line and "수신" in line: in_task = True
        if in_task and "완료" in line and task_id in line: break
        if in_task and "native tool_call:" in line:
            for t in counts:
                if t in line: counts[t] += 1
    return counts

rows = []
for label, task, inject in CASES:
    print(f"\n▶ [{label}]")
    enriched = inject_graph_context(task)
    has_ctx = enriched.startswith("[GRAPH CONTEXT]")
    node_cnt = len(re.findall(r'^- \w', enriched, re.MULTILINE)) if has_ctx else 0
    print(f"  GRAPH CONTEXT: {'✅ '+str(node_cnt)+'노드' if has_ctx else '❌'}")

    reply, elapsed, task_id = send_and_wait(enriched if has_ctx else task)
    counts = count_from_log(task_id)
    total_calls = counts["read_file"] + counts["bash"]
    print(f"  read_file={counts['read_file']} bash={counts['bash']} 총={total_calls} {elapsed:.0f}s")
    rows.append((label, has_ctx, node_cnt, counts["read_file"], counts["bash"], total_calls, f"{elapsed:.0f}s"))

print("\n" + "="*65)
print(f"{'케이스':<16} {'CTX':<5} {'노드':<4} {'read':<5} {'bash':<5} {'합계':<5} {'시간'}")
print("-"*65)
for r in rows:
    print(f"{r[0]:<16} {'✅' if r[1] else '❌':<5} {r[2]:<4} {r[3]:<5} {r[4]:<5} {r[5]:<5} {r[6]}")

ctx_rows   = [r for r in rows if r[1]]
noctx_rows = [r for r in rows if not r[1]]
if ctx_rows and noctx_rows:
    avg_ctx   = sum(r[5] for r in ctx_rows)/len(ctx_rows)
    avg_noctx = sum(r[5] for r in noctx_rows)/len(noctx_rows)
    print(f"\n평균 도구 호출 (CTX 있음): {avg_ctx:.1f}")
    print(f"평균 도구 호출 (CTX 없음): {avg_noctx:.1f}")
    print(f"탐색 비용 절감:            ≈{avg_noctx-avg_ctx:.1f}회/태스크")

print("\n【운영 지침】")
print("1. 한국어 aliases(자동매매/봇1/절전 등) 사용 시 GRAPH CONTEXT 자동 주입 → 탐색 절감")
print("2. aliases 미등록 한국어 → GRAPH CONTEXT 없음 → Qwen grep 탐색 필요")
print("3. symbol명 직접 포함(auto_trade_cycle) → 항상 매칭")
print("4. 태스크에 file:line 명시 시 → read_file 완전 생략 가능 (최적)")
print("5. 장중(08:00~20:00) 긴 수정 태스크 자제")
