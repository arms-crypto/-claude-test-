"""graphify_core.py — AST 기반 코드 그래프 생성기."""
import ast
import hashlib
import json
import os
import pathlib
import re
import time
from typing import Any

ROOT = pathlib.Path("/home/ubuntu/-claude-test-")
OUT_DIR = ROOT / "graphify-out"
DOC_DIR = OUT_DIR / "doc"
GRAPH_JSON = OUT_DIR / "graph.json"
REPORT_MD = OUT_DIR / "GRAPH_REPORT.md"

_EXTERNAL_CALL_ROOTS = {
    "requests", "urllib", "httpx", "aiohttp", "socket",
    "subprocess", "boto3", "paramiko", "ssh", "ftplib",
}
_DB_WRITE_VERBS = {"execute", "executemany", "commit"}
_DB_WRITE_FUNCS = {"write_file"}
_DB_WRITE_SQL_RE = re.compile(r'\b(INSERT|UPDATE|DELETE|MERGE|DROP|CREATE|ALTER)\b', re.IGNORECASE)
_SECRET_RE = re.compile(
    r'\b(password|secret|token|api_key|apikey|private_key|app_secret|'
    r'db_pass|wallet_pass|lm_api_key|access_key|passwd|credential)\b',
    re.IGNORECASE,
)
_KOREAN_RE = re.compile(r'[\uAC00-\uD7A3]{2,}')
_FUNC_ALIAS_RE = re.compile(
    r'([\uAC00-\uD7A3]{2,8})\s*[→:\-]\s*[`"]?([a-z_][a-zA-Z0-9_]{3,})[`"]?'
    r'|[`"]([a-z_][a-zA-Z0-9_]{3,})[`"]\s*[←:\-]\s*([\uAC00-\uD7A3]{2,8})'
)


def sha1_file(path: pathlib.Path) -> str:
    h = hashlib.sha1()
    h.update(path.read_bytes())
    return h.hexdigest()


def sha1_block(lines: list[str], start: int, end: int) -> str:
    h = hashlib.sha1()
    h.update("".join(lines[start:end]).encode())
    return h.hexdigest()


def collect_py_files(root: pathlib.Path) -> list[pathlib.Path]:
    result = []
    for p in sorted(root.rglob("*.py")):
        parts = p.parts
        if "__pycache__" in parts or ".venv" in parts or "venv" in parts:
            continue
        result.append(p)
    return result


def _call_chain_root(node: ast.AST) -> str | None:
    """ast.Call의 함수 체인 루트 이름을 반환."""
    func = getattr(node, "func", None)
    if func is None:
        return None
    while isinstance(func, ast.Attribute):
        func = func.value
    if isinstance(func, ast.Name):
        return func.id
    return None


def detect_risk_tags(node: ast.FunctionDef, source_lines: list[str]) -> list[str]:
    tags: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        root = _call_chain_root(child)
        # external-call
        if root in _EXTERNAL_CALL_ROOTS:
            tags.add("external-call")
        # db-write via .execute/.commit
        func = getattr(child, "func", None)
        if isinstance(func, ast.Attribute):
            if func.attr in _DB_WRITE_VERBS:
                if func.attr == "commit":
                    tags.add("db-write")
                else:
                    for arg in child.args:
                        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                            if _DB_WRITE_SQL_RE.search(arg.value):
                                tags.add("db-write")
                        elif isinstance(arg, ast.JoinedStr):
                            tags.add("db-write")
        if isinstance(func, ast.Name) and func.id in _DB_WRITE_FUNCS:
            tags.add("db-write")
        # secret: config.TOKEN / os.environ
        for sub in ast.walk(child):
            if isinstance(sub, ast.Attribute):
                if _SECRET_RE.search(sub.attr):
                    tags.add("secret")
                if (sub.attr == "environ" and
                        isinstance(sub.value, ast.Name) and
                        sub.value.id == "os"):
                    tags.add("secret")
    return sorted(tags)


def _find_calls(node: ast.FunctionDef, all_names: set[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = getattr(child, "func", None)
            name = None
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name and name in all_names and name not in seen and name != node.name:
                seen.add(name)
                result.append(name)
    return result


def _find_local_imports(node: ast.FunctionDef) -> list[str]:
    mods: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, (ast.Import, ast.ImportFrom)):
            if isinstance(child, ast.Import):
                for alias in child.names:
                    mods.append(alias.name.split(".")[0])
            else:
                if child.module:
                    mods.append(child.module.split(".")[0])
    return list(dict.fromkeys(mods))


def _find_config_reads(node: ast.FunctionDef) -> list[str]:
    keys: list[str] = []
    seen: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Attribute):
            if isinstance(child.value, ast.Name) and child.value.id == "config":
                if child.attr not in seen:
                    seen.add(child.attr)
                    keys.append(child.attr)
    return keys


def parse_file(path: pathlib.Path, root: pathlib.Path) -> dict[str, Any]:
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return {"symbols": {}, "file_imports": [], "source_lines": []}
    source_lines = source.splitlines(keepends=True)
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return {"symbols": {}, "file_imports": [], "source_lines": source_lines}

    rel = str(path.relative_to(root))

    # 파일 레벨 import
    file_imports: list[str] = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                file_imports.append(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            file_imports.append(node.module.split(".")[0])

    # 모든 최상위 이름 수집 (calls 필터링용)
    all_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            all_names.add(node.name)

    symbols: dict[str, Any] = {}

    def _process_funcdef(node: ast.FunctionDef | ast.AsyncFunctionDef, parent: str | None = None):
        name = node.name
        end_line = getattr(node, "end_lineno", node.lineno)
        block_sha1 = sha1_block(source_lines, node.lineno - 1, end_line)
        sym: dict[str, Any] = {
            "kind": "function",
            "file": rel,
            "line": node.lineno,
            "end_line": end_line,
            "calls": _find_calls(node, all_names),
            "called_by": [],
            "imports": _find_local_imports(node),
            "reads_config": _find_config_reads(node),
            "risk_tags": detect_risk_tags(node, source_lines),
            "sha1": block_sha1,
        }
        if parent:
            sym["parent"] = parent
        symbols[name] = sym

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _process_funcdef(node)
        elif isinstance(node, ast.ClassDef):
            end_line = getattr(node, "end_lineno", node.lineno)
            symbols[node.name] = {
                "kind": "class",
                "file": rel,
                "line": node.lineno,
                "end_line": end_line,
                "calls": [],
                "called_by": [],
                "imports": [],
                "reads_config": [],
                "risk_tags": [],
                "sha1": sha1_block(source_lines, node.lineno - 1, end_line),
            }
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    _process_funcdef(child, parent=node.name)

    return {"symbols": symbols, "file_imports": list(dict.fromkeys(file_imports)), "source_lines": source_lines}


_COMMON_KOREAN = {
    "도구", "실행", "금지", "패스", "명시", "설정", "작업", "적용", "방법",
    "파일", "수정", "확인", "사용", "제거", "추가", "처리", "반환", "호출",
    "코드", "모델", "서버", "요청", "결과", "정의", "상태", "타입", "변수",
}


def _extract_aliases_from_claude_md(root: pathlib.Path, known_symbols: set[str] = None) -> dict[str, list[str]]:
    claude_md = root / "CLAUDE.md"
    if not claude_md.exists():
        return {}
    text = claude_md.read_text(encoding="utf-8", errors="replace")
    aliases: dict[str, list[str]] = {}
    for m in _FUNC_ALIAS_RE.finditer(text):
        korean = m.group(1) or m.group(4)
        func = m.group(2) or m.group(3)
        if not (korean and func):
            continue
        if korean in _COMMON_KOREAN:
            continue
        if known_symbols is not None and func not in known_symbols:
            continue
        aliases.setdefault(korean, [])
        if func not in aliases[korean]:
            aliases[korean].append(func)
    return aliases


def _detect_entrypoints(symbols: dict, files_meta: dict) -> list[str]:
    entries: list[str] = []
    for name, sym in symbols.items():
        if sym["kind"] != "function":
            continue
        if name.startswith("_"):
            continue
        if sym["called_by"]:
            continue
        # 파일에 if __name__ == '__main__' 체크는 생략 — 충분히 필터됨
        entries.append(name)
    # 너무 많으면 known 진입점만
    KNOWN = {"main", "auto_trade_loop", "handle_tg", "handle_tg_srv",
              "main_loop", "run", "start", "app"}
    filtered = [e for e in entries if e in KNOWN]
    return filtered if filtered else entries[:10]


def build_graph(root: pathlib.Path = ROOT) -> dict:
    py_files = collect_py_files(root)
    all_symbols: dict[str, Any] = {}
    files_meta: dict[str, Any] = {}

    for path in py_files:
        rel = str(path.relative_to(root))
        parsed = parse_file(path, root)
        all_symbols.update(parsed["symbols"])
        files_meta[rel] = {
            "summary": _infer_file_summary(rel, parsed["symbols"]),
            "top_symbols": list(parsed["symbols"].keys())[:10],
            "imports": parsed["file_imports"],
            "mtime": path.stat().st_mtime,
            "sha1": sha1_file(path),
        }

    # 2-pass: called_by 역인덱스
    for sym_name, sym in all_symbols.items():
        for callee in sym["calls"]:
            if callee in all_symbols and sym_name not in all_symbols[callee]["called_by"]:
                all_symbols[callee]["called_by"].append(sym_name)

    aliases = _extract_aliases_from_claude_md(root, set(all_symbols.keys()))
    entrypoints = _detect_entrypoints(all_symbols, files_meta)

    graph = {
        "meta": {
            "root": str(root),
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "python_files": len(py_files),
            "total_symbols": len(all_symbols),
            "entrypoints": entrypoints,
        },
        "symbols": all_symbols,
        "files": files_meta,
        "aliases": aliases,
    }
    return graph


def _infer_file_summary(rel: str, symbols: dict) -> str:
    name = pathlib.Path(rel).stem
    tops = list(symbols.keys())[:3]
    hint = ", ".join(tops) if tops else "—"
    summaries = {
        "auto_trader": "자동매매 루프 및 매수/매도 실행",
        "llm_client": "LLM 호출, WoL, 도구 정의",
        "telegram_bots": "텔레그램 봇 핸들러",
        "ai_chat": "ask_ai() 핵심 로직",
        "config": "전역 설정값 및 상태 변수",
        "db_utils": "Oracle DB 연결 및 쿼리 유틸리티",
        "search_utils": "SearXNG / Perplexica 검색",
        "rag_store": "RAG Chroma 벡터 저장소",
        "stock_data": "KRX 주가/OHLCV 데이터 수집",
        "sector_params": "업종별 매매 파라미터",
        "mock_trading/mock_trading": "가상/실전 포트폴리오 매매 로직",
        "mock_trading/kis_client": "KIS 실전 API 클라이언트 (트레이너 계좌)",
        "mock_trading/kis_client_ky": "KIS 실전 API 클라이언트 (KY 계좌)",
        "error_monitor": "로그 감시 + 텔레그램 에러 알림",
        "error_dashboard": "에러 대시보드 웹 UI",
        "pc_director": "PC LLM 관리자 — 일일 전략 JSON 생성",
        "서버보수에이전트": "Qwen 태스크 서버 (port 8001) + 텔레그램 봇",
        "proxy_v54": "Flask 메인 서버 (port 11435)",
    }
    return summaries.get(name, f"{name} ({hint})")


def is_stale(root: pathlib.Path = ROOT) -> bool:
    if not GRAPH_JSON.exists():
        return True
    try:
        graph = json.loads(GRAPH_JSON.read_text())
    except Exception:
        return True
    for rel, fmeta in graph.get("files", {}).items():
        path = root / rel
        if not path.exists():
            return True
        if abs(path.stat().st_mtime - fmeta.get("mtime", 0)) > 1:
            return True
    return False


def write_outputs(graph: dict, root: pathlib.Path = ROOT) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    DOC_DIR.mkdir(parents=True, exist_ok=True)
    GRAPH_JSON.write_text(json.dumps(graph, ensure_ascii=False, indent=2))
    REPORT_MD.write_text(_render_report(graph))
    for rel in graph["files"]:
        doc_path = DOC_DIR / (rel.replace("/", "__") + ".md")
        doc_path.write_text(_render_file_doc(rel, graph))


def _render_report(graph: dict) -> str:
    meta = graph["meta"]
    symbols = graph["symbols"]
    files = graph["files"]
    aliases = graph["aliases"]

    lines: list[str] = []
    lines.append("# Code Graph Report")
    lines.append(f"Generated: {meta['generated_at']}  |  "
                 f"Files: {meta['python_files']}  |  "
                 f"Symbols: {meta['total_symbols']}")
    lines.append("")
    lines.append("---")
    lines.append("")

    # 1. 진입점
    lines.append("## 1. Entrypoints")
    lines.append("")
    lines.append("| Symbol | File | Line |")
    lines.append("|--------|------|------|")
    for ep in meta.get("entrypoints", []):
        sym = symbols.get(ep, {})
        lines.append(f"| {ep} | {sym.get('file','?')} | {sym.get('line','?')} |")
    lines.append("")

    # 2. 파일 요약
    lines.append("## 2. Files Overview")
    lines.append("")
    lines.append("| File | Lines | Symbols | SHA1(8) | Risk Tags |")
    lines.append("|------|-------|---------|---------|-----------|")
    for rel, fmeta in sorted(files.items()):
        # 라인 수: end_line 최대값
        file_syms = [s for s in symbols.values() if s["file"] == rel]
        max_line = max((s["end_line"] for s in file_syms), default=0)
        sym_count = len(file_syms)
        sha_short = fmeta["sha1"][:8]
        risk_set: set[str] = set()
        for s in file_syms:
            risk_set.update(s.get("risk_tags", []))
        risk_str = ", ".join(sorted(risk_set)) if risk_set else "—"
        lines.append(f"| {rel} | {max_line} | {sym_count} | {sha_short} | {risk_str} |")
    lines.append("")

    # 3. 심볼 인덱스 — 핵심 파일만 (상위 5개 파일, 파일당 3개)
    TOP_FILES = {"auto_trader.py", "llm_client.py", "서버보수에이전트.py",
                 "ai_chat.py", "telegram_bots.py", "mock_trading/kis_client.py"}
    lines.append("## 3. Key Symbol Index (top files only)")
    lines.append("*전체 목록 → `graphify-out/doc/`*")
    lines.append("")
    for rel in sorted(files.keys()):
        base = pathlib.Path(rel).name
        if base not in TOP_FILES and rel not in TOP_FILES:
            continue
        file_syms = [(n, s) for n, s in symbols.items() if s["file"] == rel]
        if not file_syms:
            continue
        lines.append(f"### {rel}")
        lines.append("| Function | Line | Calls | Risk |")
        lines.append("|----------|------|-------|------|")
        for name, sym in file_syms[:3]:
            calls_str = ", ".join(sym["calls"][:3]) or "—"
            risk_str = ", ".join(sym["risk_tags"]) or "—"
            lines.append(f"| {name} | {sym['line']} | {calls_str} | {risk_str} |")
        doc_link = rel.replace('/', '__') + '.md'
        lines.append(f"*→ [상세](graphify-out/doc/{doc_link})*")
        lines.append("")

    # 4. Risk Tag 요약
    lines.append("## 4. Risk Tag Summary")
    lines.append("")
    for tag in ("external-call", "db-write", "secret"):
        tagged = [(n, s) for n, s in symbols.items() if tag in s.get("risk_tags", [])]
        if not tagged:
            continue
        lines.append(f"### {tag} ({len(tagged)})")
        lines.append("")
        lines.append("| Symbol | File | Line |")
        lines.append("|--------|------|------|")
        for name, sym in tagged[:15]:
            lines.append(f"| {name} | {sym['file']} | {sym['line']} |")
        lines.append("")

    # 5. Top Call Hubs
    lines.append("## 5. Top Call Hubs")
    lines.append("")
    lines.append("| Rank | Symbol | File | Callers |")
    lines.append("|------|--------|------|---------|")
    hubs = sorted(symbols.items(), key=lambda x: len(x[1]["called_by"]), reverse=True)
    for i, (name, sym) in enumerate(hubs[:20], 1):
        if not sym["called_by"]:
            break
        callers = ", ".join(sym["called_by"][:5])
        lines.append(f"| {i} | {name} | {sym['file']} | {callers} |")
    lines.append("")

    # 6. Config 의존성
    lines.append("## 6. Config Dependency Map")
    lines.append("")
    lines.append("| Symbol | File | Config Keys |")
    lines.append("|--------|------|-------------|")
    for name, sym in sorted(symbols.items()):
        if sym.get("reads_config"):
            keys_str = ", ".join(sym["reads_config"][:5])
            lines.append(f"| {name} | {sym['file']} | {keys_str} |")
    lines.append("")

    # 7. Korean Aliases
    if aliases:
        lines.append("## 7. Korean Aliases")
        lines.append("")
        lines.append("| Korean | Resolves To |")
        lines.append("|--------|-------------|")
        for korean, funcs in aliases.items():
            lines.append(f"| {korean} | {', '.join(funcs)} |")
        lines.append("")

    lines.append("---")
    lines.append("*Regenerate: `python3 graphify.py .`*  ")
    lines.append("*Per-file detail: `graphify-out/doc/{filename}.md`*")

    return "\n".join(lines)


def _render_file_doc(rel: str, graph: dict) -> str:
    symbols = graph["symbols"]
    fmeta = graph["files"].get(rel, {})
    file_syms = [(n, s) for n, s in symbols.items() if s["file"] == rel]

    lines: list[str] = []
    lines.append(f"# {rel}")
    lines.append("")
    lines.append(f"**Summary**: {fmeta.get('summary', '—')}")
    lines.append(f"**SHA1**: {fmeta.get('sha1', '?')[:16]}  "
                 f"**mtime**: {fmeta.get('mtime', 0):.0f}")
    lines.append("")
    lines.append("| Symbol | Kind | Line | End | Calls | Called By | Risk |")
    lines.append("|--------|------|------|-----|-------|-----------|------|")
    for name, sym in sorted(file_syms, key=lambda x: x[1]["line"]):
        calls_str = ", ".join(sym["calls"][:5]) or "—"
        cb_str = ", ".join(sym["called_by"][:5]) or "—"
        risk_str = ", ".join(sym["risk_tags"]) or "—"
        lines.append(f"| {name} | {sym['kind']} | {sym['line']} | {sym['end_line']} "
                     f"| {calls_str} | {cb_str} | {risk_str} |")
    return "\n".join(lines)


def run_build(root: pathlib.Path = ROOT) -> dict:
    graph = build_graph(root)
    write_outputs(graph, root)
    return graph


if __name__ == "__main__":
    g = run_build()
    print(f"Done: {g['meta']['total_symbols']} symbols, "
          f"{g['meta']['python_files']} files → {GRAPH_JSON}")
