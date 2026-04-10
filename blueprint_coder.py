#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
blueprint_coder.py — Claude 설계도 → Mistral 구현/검토 → 코드 출력

역할 분리:
  Claude  : 설계도(blueprint) 작성 → 최종 확인만
  Mistral : 작업자(Worker) + 검토자(Reviewer) 역할 분리

사용법:
  python3 blueprint_coder.py blueprint.md          # 파일로 실행
  python3 blueprint_coder.py blueprint.md -o out.py  # 출력 파일 지정
  python3 blueprint_coder.py blueprint.md --no-review  # 리뷰 스킵

  또는 모듈로:
    from blueprint_coder import run
    code = run(blueprint_text, output_file="sector_params.py")
"""

import os, sys, re, json, time, logging, argparse, requests, datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger("blueprint_coder")

OLLAMA_PC   = "http://221.144.111.116:11434/api/chat"
MODEL_PC    = "mistral-small3.1:24b"
MAX_RETRIES = 2   # 리뷰 실패 시 최대 재구현 횟수

# ── CLAUDE.md 로드 ─────────────────────────────────────────────────────────────

def _load_claude_md() -> str:
    """CLAUDE.md에서 핵심 섹션만 추출 (전체 주입 시 토큰 낭비 방지)."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "CLAUDE.md")
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()
        # 핵심 섹션만 추출: 핵심 파일, 핵심 설정값, 구조 요약, 절대 하지 말 것
        keep = []
        current = []
        target_sections = {"## 핵심 파일", "## 핵심 설정값", "## 구조 요약", "## 절대 하지 말 것", "## 자주 쓰는 명령"}
        capturing = False
        for line in content.splitlines():
            if any(line.startswith(s) for s in target_sections):
                capturing = True
            elif line.startswith("## ") and capturing:
                keep.extend(current)
                current = []
                capturing = False
            if capturing:
                current.append(line)
        keep.extend(current)
        return "\n".join(keep)
    except Exception:
        return ""

_PROJECT_CONTEXT = ""
_claude_ctx = _load_claude_md()
if _claude_ctx:
    _PROJECT_CONTEXT = f"## 프로젝트 컨텍스트 (CLAUDE.md 핵심 요약)\n{_claude_ctx}\n\n"

# ── 시스템 프롬프트 ────────────────────────────────────────────────────────────

_WORKER_SYSTEM = """너는 시니어 Python 개발자야.
주어진 설계도(blueprint)를 보고 코드를 구현해.

규칙:
- Python 코드만 출력 (설명, 마크다운 코드블록 제외)
- 설계도에 명시된 함수/클래스/인터페이스를 정확히 구현
- 기존 코드베이스 패턴을 따를 것 (설계도에 명시된 경우)
- 주석은 한국어, 코드는 영어
- 절대로 설명 텍스트를 코드 앞뒤에 붙이지 말 것"""

_REVIEWER_SYSTEM = """너는 Python 코드 리뷰어야.
주어진 설계도와 구현 코드를 비교해서 문제점만 찾아.

검토 항목:
1. 설계도의 함수/인터페이스가 모두 구현됐는가?
2. 명백한 버그 (NameError, 타입 오류, 무한루프 등)
3. 설계도와 다르게 구현된 부분

반드시 JSON으로만 반환:
{
  "ok": true/false,
  "issues": ["문제1", "문제2"],
  "critical": true/false
}

ok=true면 이슈 없음. critical=true면 재구현 필요."""


# ── Ollama 호출 ───────────────────────────────────────────────────────────────

def _call(messages: list, system: str, timeout: int = 300) -> str:
    """PC Ollama(Mistral)만 사용. PC 꺼져있으면 실패 반환."""
    try:
        payload = {
            "model": MODEL_PC,
            "messages": [{"role": "system", "content": system}] + messages,
            "stream": False,
            "options": {"temperature": 0.2},
        }
        r = requests.post(OLLAMA_PC, json=payload,
                          timeout=(5, timeout),
                          proxies={"http": None, "https": None})
        r.raise_for_status()
        content = r.json()["message"]["content"].strip()
        logger.info("[%s] 응답 %d자", MODEL_PC, len(content))
        return content
    except Exception as e:
        logger.error("PC Ollama 호출 실패: %s", e)
        logger.error("PC가 꺼져있거나 Ollama가 실행 중이지 않습니다.")
        return ""


# ── 작업자: 코드 구현 ─────────────────────────────────────────────────────────

def worker(blueprint: str, feedback: str = "") -> str:
    """설계도 → 코드 구현."""
    logger.info("▶ [Worker] 코드 구현 시작")
    prompt = f"{_PROJECT_CONTEXT}## 설계도\n{blueprint}"
    if feedback:
        prompt += f"\n\n## 이전 리뷰 피드백 (반드시 반영)\n{feedback}"
    prompt += "\n\n위 설계도대로 Python 코드를 구현해. 코드만 출력."

    raw = _call([{"role": "user", "content": prompt}], _WORKER_SYSTEM)
    # 마크다운 코드블록 제거
    code = re.sub(r"^```(?:python)?\n?", "", raw, flags=re.MULTILINE)
    code = re.sub(r"\n?```$", "", code, flags=re.MULTILINE)
    return code.strip()


# ── 검토자: 코드 리뷰 ─────────────────────────────────────────────────────────

def reviewer(blueprint: str, code: str) -> dict:
    """구현 코드를 설계도 기준으로 검토."""
    logger.info("▶ [Reviewer] 코드 검토 시작")
    prompt = (
        f"{_PROJECT_CONTEXT}## 설계도\n{blueprint}\n\n"
        f"## 구현 코드\n```python\n{code}\n```\n\n"
        "위 코드를 설계도 기준으로 검토해. JSON만 반환."
    )
    raw = _call([{"role": "user", "content": prompt}], _REVIEWER_SYSTEM, timeout=120)

    try:
        m = re.search(r'\{[^{}]+\}', raw, re.DOTALL)
        if m:
            result = json.loads(m.group())
            issues = result.get("issues", [])
            logger.info("[Reviewer] ok=%s critical=%s issues=%d개",
                        result.get("ok"), result.get("critical"), len(issues))
            for issue in issues:
                logger.info("  ⚠ %s", issue)
            return result
    except Exception as e:
        logger.warning("[Reviewer] JSON 파싱 실패: %s | raw=%s", e, raw[:200])

    return {"ok": True, "issues": [], "critical": False}


# ── 메인 오케스트레이터 ───────────────────────────────────────────────────────

def run(blueprint: str, output_file: str = "", do_review: bool = True) -> str:
    """
    설계도 → Mistral 구현/검토 반복 → 최종 코드 반환.

    Args:
        blueprint  : 설계도 텍스트 (Claude가 작성)
        output_file: 결과 저장 파일 경로 (빈 문자열이면 저장 안 함)
        do_review  : False면 리뷰 스킵 (빠른 초안 필요 시)

    Returns:
        최종 코드 문자열
    """
    start = time.time()
    logger.info("=" * 60)
    logger.info("blueprint_coder 시작 | 리뷰=%s | 출력=%s",
                do_review, output_file or "(저장 안 함)")
    logger.info("=" * 60)

    code = worker(blueprint)
    if not code:
        logger.error("Worker 코드 생성 실패")
        return ""

    if do_review:
        for attempt in range(1, MAX_RETRIES + 1):
            result = reviewer(blueprint, code)
            if result.get("ok") or not result.get("critical"):
                if result.get("issues"):
                    logger.info("[Reviewer] 경미한 이슈 %d개 — 통과", len(result["issues"]))
                else:
                    logger.info("[Reviewer] ✅ 이슈 없음 — 완료")
                break
            # critical 이슈 → 피드백 포함해서 재구현
            feedback = "\n".join(f"- {i}" for i in result["issues"])
            logger.info("[%d/%d] critical 이슈 → Worker 재구현", attempt, MAX_RETRIES)
            code = worker(blueprint, feedback=feedback)
            if not code:
                logger.error("재구현 실패")
                break

    elapsed = time.time() - start
    logger.info("완료 (%.1f초) | 코드 %d줄", elapsed, code.count("\n") + 1)

    if output_file:
        # 기존 파일 백업
        if os.path.exists(output_file):
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            backup = f"{output_file}.bak_{ts}"
            os.rename(output_file, backup)
            logger.info("기존 파일 백업: %s", backup)
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(code)
        logger.info("저장 완료: %s", output_file)

    return code


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Blueprint → Mistral 코드 생성")
    parser.add_argument("blueprint", help="설계도 파일 경로 (.md)")
    parser.add_argument("-o", "--output", default="", help="출력 파일 경로")
    parser.add_argument("--no-review", action="store_true", help="리뷰 스킵")
    parser.add_argument("--print", action="store_true", dest="print_code",
                        help="코드를 stdout에 출력")
    args = parser.parse_args()

    if not os.path.exists(args.blueprint):
        print(f"오류: 파일 없음 → {args.blueprint}")
        sys.exit(1)

    with open(args.blueprint, encoding="utf-8") as f:
        blueprint = f.read()

    code = run(blueprint, output_file=args.output, do_review=not args.no_review)

    if args.print_code and code:
        print("\n" + "=" * 60)
        print(code)


if __name__ == "__main__":
    main()
