"""graphify_wrapper.py — Qwen 태스크 텍스트에 graph context를 prepend."""
import json
import os
import pathlib
import re
from typing import Optional

GRAPH_JSON = pathlib.Path("/home/ubuntu/-claude-test-/graphify-out/graph.json")
MAX_NODES = 10
_CONTEXT_HEADER = "[GRAPH CONTEXT]"
_CONTEXT_FOOTER = "[/GRAPH CONTEXT]"

_SKIP_WORDS = {
    "def", "class", "import", "from", "return", "True", "False", "None",
    "for", "while", "try", "except", "with", "self", "args", "kwargs",
    "print", "len", "str", "int", "list", "dict", "set", "type", "range",
    "open", "read", "write", "line", "file", "path", "data", "result",
    "value", "key", "name", "text", "info", "log", "msg", "err", "res",
}

_graph_cache: Optional[dict] = None
_graph_mtime: float = 0.0


def _load_graph() -> Optional[dict]:
    global _graph_cache, _graph_mtime
    if not GRAPH_JSON.exists():
        return None
    try:
        mtime = GRAPH_JSON.stat().st_mtime
        if _graph_cache is not None and abs(mtime - _graph_mtime) < 1:
            return _graph_cache
        _graph_cache = json.loads(GRAPH_JSON.read_text(encoding="utf-8"))
        _graph_mtime = mtime
        return _graph_cache
    except Exception:
        return None


def _extract_candidate_symbols(task_text: str, graph: dict) -> list[str]:
    known = set(graph["symbols"].keys())
    aliases = graph.get("aliases", {})

    found: list[str] = []
    seen: set[str] = set()

    # Pass 1: Python 식별자 (최소 3자)
    for m in re.finditer(r'\b([a-zA-Z_][a-zA-Z0-9_]{2,})\b', task_text):
        name = m.group(1)
        if name in _SKIP_WORDS:
            continue
        if name in known and name not in seen:
            found.append(name)
            seen.add(name)

    # Pass 2: 한글 alias 역조회
    for m in re.finditer(r'[\uAC00-\uD7A3]{2,}', task_text):
        korean = m.group(0)
        if korean in aliases:
            for sym in aliases[korean]:
                if sym in known and sym not in seen:
                    found.append(sym)
                    seen.add(sym)

    # Pass 3: 파일명 → 해당 파일 심볼 추가
    for m in re.finditer(r'[\w\uAC00-\uD7A3]+\.py\b', task_text):
        fname = m.group(0)
        for sym_name, sym_data in graph["symbols"].items():
            if sym_data.get("file", "").endswith(fname) and sym_name not in seen:
                found.append(sym_name)
                seen.add(sym_name)

    return found


def _score_symbol(name: str, task_text: str, graph: dict) -> int:
    sym = graph["symbols"].get(name, {})
    score = 0
    if re.search(r'\b' + re.escape(name) + r'\b', task_text):
        score += 3
    if sym.get("risk_tags"):
        score += 2
    if name in graph.get("meta", {}).get("entrypoints", []):
        score += 1
    if sym.get("called_by"):
        score += 1
    return score


def _format_node(name: str, sym: dict) -> str:
    calls = sym.get("calls", [])[:5]
    called_by = sym.get("called_by", [])[:5]
    risk = sym.get("risk_tags", [])
    config_keys = sym.get("reads_config", [])[:3]

    parts = [f"- {name} ({sym.get('kind','fn')}) @ {sym.get('file','?')}:{sym.get('line','?')}-{sym.get('end_line','?')}"]
    if calls:
        parts.append(f"  calls: {', '.join(calls)}")
    if called_by:
        parts.append(f"  called_by: {', '.join(called_by)}")
    if risk:
        parts.append(f"  risk: {', '.join(risk)}")
    if config_keys:
        parts.append(f"  config: {', '.join(config_keys)}")
    return "\n".join(parts)


def inject_graph_context(task_text: str) -> str:
    graph = _load_graph()
    if graph is None:
        return task_text

    candidates = _extract_candidate_symbols(task_text, graph)
    if not candidates:
        return task_text

    # 점수 정렬
    scored = sorted(candidates, key=lambda n: _score_symbol(n, task_text, graph), reverse=True)

    # neighbors 1단계 확장
    selected: list[str] = []
    seen: set[str] = set()
    for name in scored:
        if len(selected) >= MAX_NODES:
            break
        if name not in seen:
            selected.append(name)
            seen.add(name)
        sym = graph["symbols"].get(name, {})
        for neighbor in sym.get("calls", []) + sym.get("called_by", []):
            if len(selected) >= MAX_NODES:
                break
            if neighbor in graph["symbols"] and neighbor not in seen:
                selected.append(neighbor)
                seen.add(neighbor)

    node_lines = [_format_node(n, graph["symbols"][n]) for n in selected]
    context_block = (
        f"{_CONTEXT_HEADER}\n"
        + "\n".join(node_lines)
        + f"\n{_CONTEXT_FOOTER}"
    )
    return context_block + "\n\n" + task_text


def strip_graph_context(text: str) -> str:
    return re.sub(
        r'\[GRAPH CONTEXT\].*?\[/GRAPH CONTEXT\]\n*',
        '',
        text,
        flags=re.DOTALL,
    ).lstrip()
