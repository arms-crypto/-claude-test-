#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
llm_client.py — LLM + WoL
send_wol(), wait_for_ollama(), _parse_ollama_response(),
Tool 상수들, _execute_tool_call(), call_mistral_only(),
call_qwen (= call_mistral_only), _ollama_alive()
"""

import os
import json
import time
import threading
import requests

import config

logger = config.logger


# -------------------------
# Wake on LAN
def send_wol():
    """Wake on LAN: 라우터 SSH ether-wake (1순위) + UDP 직접 전송 (2순위)."""
    import subprocess
    mac = config.WOL_MAC  # 예: 3C:7C:3F:F2:B0:41
    # PC가 이미 응답 중이면 WoL 스킵
    try:
        requests.get("http://221.144.111.116:8000/v1/models", headers=_LM_HEADERS, timeout=2, proxies={"http": None, "https": None})
        return False
    except Exception:
        pass

    # 1순위: 라우터 SSH로 ether-wake (LAN에서 직접 전송 → 2차 절전도 깨어남)
    try:
        result = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
             "-p", "2222", "-i", "/home/ubuntu/.ssh/id_rsa",
             "qflavor12@221.144.111.116",
             f"ether-wake -i br0 {mac}"],
            capture_output=True, timeout=10
        )
        if result.returncode == 0:
            logger.info("WoL ether-wake (라우터 SSH) 전송 완료")
            config.WOL_SENT = True
            return True
        logger.warning("ether-wake 실패: %s", result.stderr.decode()[:100])
    except Exception as e:
        logger.warning("라우터 SSH WoL 실패: %s", e)

    # 2순위: UDP 직접 전송 (폴백)
    try:
        _mac = mac.replace(":", "").replace("-", "")
        magic = bytes.fromhex("F" * 12 + _mac * 16)
        with __import__("socket").socket(__import__("socket").AF_INET, __import__("socket").SOCK_DGRAM) as s:
            s.setsockopt(__import__("socket").SOL_SOCKET, __import__("socket").SO_BROADCAST, 1)
            for _ in range(5):
                s.sendto(magic, (config.WOL_IP, 9))
                s.sendto(magic, (config.WOL_IP, 7))
        logger.info("WoL UDP 폴백 전송 완료 → %s", config.WOL_IP)
        config.WOL_SENT = True
        return True
    except Exception as e:
        logger.error("WoL UDP 폴백 실패: %s", e)
        return False


_LM_HEADERS = {"Authorization": f"Bearer {config.LM_API_KEY}"}

import time as _time_mod
_last_ollama_request = [_time_mod.time()]  # 마지막 Ollama 요청 시각 (시작 시각으로 초기화)


def touch_ollama_request():
    """슬립 타이머 리셋 — Ollama 비사용 작업(스캔 등) 시작 전 호출"""
    _last_ollama_request[0] = _time_mod.time()


_PS_IDLE_SCRIPT = "/tmp/get_idle.ps1"
_PS_IDLE_REMOTE = "C:/temp/get_idle.ps1"
_PS_IDLE_CONTENT = r"""Add-Type @"
using System;
using System.Runtime.InteropServices;
public class IdleTime {
    [DllImport("user32.dll")]
    static extern bool GetLastInputInfo(ref LASTINPUTINFO plii);
    [StructLayout(LayoutKind.Sequential)]
    public struct LASTINPUTINFO { public uint cbSize; public uint dwTime; }
    public static int GetIdleMinutes() {
        var info = new LASTINPUTINFO();
        info.cbSize = (uint)Marshal.SizeOf(info);
        GetLastInputInfo(ref info);
        return (int)((Environment.TickCount - info.dwTime) / 60000);
    }
}
"@
[IdleTime]::GetIdleMinutes()
"""

def _ensure_idle_script():
    """로컬에 PS1 스크립트 파일이 없으면 생성 후 PC에 업로드."""
    import subprocess, os
    if not os.path.exists(_PS_IDLE_SCRIPT):
        with open(_PS_IDLE_SCRIPT, "w") as f:
            f.write(_PS_IDLE_CONTENT)
    subprocess.run(
        ["scp", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
         "-P", "2224", "-i", "/home/ubuntu/.ssh/id_ed25519",
         _PS_IDLE_SCRIPT, f"ultimate@221.144.111.116:{_PS_IDLE_REMOTE}"],
        capture_output=True, timeout=10
    )

def _get_pc_user_idle_min() -> int:
    """
    PC Windows 사용자 유휴 시간(분) 반환.
    PowerShell GetLastInputInfo API로 마지막 입력 후 경과 시간 측정.
    확인 실패 → 0 반환 (절전 차단).
    """
    import subprocess
    try:
        _ensure_idle_script()
        result = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
             "-p", "2224", "-i", "/home/ubuntu/.ssh/id_ed25519",
             "ultimate@221.144.111.116",
             f"powershell -ExecutionPolicy Bypass -File {_PS_IDLE_REMOTE}"],
            capture_output=True, timeout=15
        )
        return int(result.stdout.decode("utf-8", errors="ignore").strip())
    except Exception as e:
        logger.debug("PC 유휴 확인 실패: %s", e)
        return 0  # SSH 실패 → 안전하게 차단


def _get_pc_load() -> tuple:
    """PC GPU/CPU 사용률(%) 반환. (gpu%, cpu%) 튜플. 확인 실패 → (0, 0)."""
    import subprocess
    try:
        cmd = "nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits"
        r = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
             "-p", "2224", "-i", "/home/ubuntu/.ssh/id_ed25519",
             "ultimate@221.144.111.116", cmd],
            capture_output=True, timeout=10
        )
        if r.returncode == 0:
            out = r.stdout.decode("utf-8", errors="ignore")
            lines = [l.strip() for l in out.splitlines() if l.strip()]
            gpu = 0
            cpu = 0
            for l in lines:
                if "LoadPercentage=" in l:
                    try:
                        cpu = int(l.split("=")[1])
                    except Exception:
                        pass
                elif l.isdigit():
                    gpu = int(l)
            return gpu, cpu
    except Exception as e:
        logger.debug("PC 로드 확인 실패: %s", e)
    return 0, 0


_load_low_since = [0.0]  # 로드가 임계값 아래로 내려간 시점 (타이머)


def send_sleep(delay_min: int = 20):
    """
    PC에 최대절전(hibernate) 명령 전송.
    - 서버 LLM 호출이 delay_min분 이내면 차단 (Ubuntu 작업 중)
    - 사용자 유휴 < 20분이면 차단 (PC 직접 사용 중)
    - GPU > 10%이면 타이머 리셋
    - 로드가 delay_min분 연속으로 낮으면 절전 실행
    """
    import subprocess, time

    # Ubuntu 서버에서 최근 LLM 호출이 있으면 절전 차단
    elapsed_since_llm = (time.time() - _last_ollama_request[0]) / 60
    if elapsed_since_llm < delay_min:
        logger.info("send_sleep 스킵 — 마지막 LLM 호출 %.0f분 전 (서버 활성)", elapsed_since_llm)
        return False

    # PC 사용자가 직접 쓰고 있는지 확인
    pc_idle_min = _get_pc_user_idle_min()
    if pc_idle_min < 20:
        logger.info("send_sleep 스킵 — PC 사용자 유휴 %d분 (직접 사용 중)", pc_idle_min)
        _load_low_since[0] = 0.0
        return False

    # GPU/CPU 로드 확인
    gpu_load, cpu_load = _get_pc_load()
    if gpu_load > 10:
        logger.info("send_sleep 타이머 리셋 — GPU %d%% / CPU %d%% (작업 중)", gpu_load, cpu_load)
        _load_low_since[0] = 0.0
        return False

    # 로드 낮음 — 타이머 시작 또는 경과 확인
    now = time.time()
    if _load_low_since[0] == 0.0:
        _load_low_since[0] = now
        logger.info("절전 타이머 시작 — GPU %d%% / CPU %d%% (기준 %d분)", gpu_load, cpu_load, delay_min)
        return False

    low_for_min = (now - _load_low_since[0]) / 60
    if low_for_min < delay_min:
        logger.info("절전 타이머 %.0f/%d분 경과 — GPU %d%% / CPU %d%%", low_for_min, delay_min, gpu_load, cpu_load)
        return False

    # delay_min분 연속 저로드 → 절전 실행
    try:
        result = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
             "-p", "2224", "-i", "/home/ubuntu/.ssh/id_ed25519",
             "ultimate@221.144.111.116",
             "schtasks /run /tn RemoteHibernate"],
            capture_output=True, timeout=10
        )
        if result.returncode == 0:
            logger.info("PC 최대절전 명령 전송 완료 (저로드 %.0f분 / PC유휴 %d분)", low_for_min, pc_idle_min)
            _load_low_since[0] = 0.0
            touch_ollama_request()  # 재전송 방지
            return True
        logger.warning("최대절전 명령 실패: %s", result.stderr.decode()[:100])
    except Exception as e:
        logger.warning("send_sleep 실패: %s", e)
    return False


def wait_for_ollama(timeout: int = 120, interval: int = 10) -> bool:
    """LM Studio (localhost:8000)가 응답할 때까지 대기. timeout초 내에 응답하면 True."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = requests.get("http://221.144.111.116:8000/v1/models", headers=_LM_HEADERS, timeout=5)
            if r.status_code == 200:
                logger.info("LM Studio 응답 확인 — 서버 정상")
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def _clean_llm_output(text: str) -> str:
    """Gemma 등 모델의 특수 토큰 제거."""
    import re
    # <unused0>~<unusedN>, <pad>, <eos>, <bos> 등 특수 토큰 제거
    text = re.sub(r'<unused\d+>', '', text)
    text = re.sub(r'<(pad|eos|bos|unk|sep|cls|mask)>', '', text)
    return text.strip()


def _parse_ollama_response(r) -> str:
    """응답에서 텍스트 추출 (Ollama/OpenAI 호환 모두 처리)."""
    raw_text = r.text.strip()
    try:
        data = r.json()
        if isinstance(data, dict):
            # OpenAI 형식: {"choices": [{"message": {"content": "..."}}]}
            if "choices" in data:
                choices = data.get("choices", [])
                if choices and isinstance(choices[0], dict):
                    return _clean_llm_output(choices[0].get("message", {}).get("content", ""))
            # Ollama 형식: {"message": {"content": "..."}}
            if "message" in data:
                return _clean_llm_output(data["message"]["content"])
            if "response" in data:
                return _clean_llm_output(data["response"])
        if isinstance(data, list):
            parts = []
            for item in data:
                if isinstance(item, dict):
                    if "message" in item:
                        parts.append(item["message"].get("content", ""))
                    elif "response" in item:
                        parts.append(item["response"])
            return _clean_llm_output("\n".join(parts))
        return str(data)
    except (json.JSONDecodeError, ValueError):
        # NDJSON (여러 줄 JSON) 처리
        for line in reversed(raw_text.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    if "message" in obj:
                        return _clean_llm_output(obj["message"].get("content", ""))
                    if "response" in obj:
                        return _clean_llm_output(obj["response"])
            except Exception:
                continue
        return _clean_llm_output(raw_text)


# -------------------------
# Tool 상수들
_WEB_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "실시간 웹 검색. 최신 뉴스, 현재 정보, 모르는 사실, 훈련 데이터 이후 사건에 사용.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "검색어 (한국어 또는 영어)"}
            },
            "required": ["query"]
        }
    }
}

_STOCK_PRICE_TOOL = {
    "type": "function",
    "function": {
        "name": "get_stock_price",
        "description": "주식 현재가 실시간 조회. 종목명(한국어/영어)이나 티커(NVDA, 005930 등)로 조회. 주가·시세·현재가 질문에 반드시 사용.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "종목명 또는 티커. 예: '엔비디아', 'NVDA', 'SK하이닉스', '000660'"}
            },
            "required": ["query"]
        }
    }
}

_NEWS_TOOL = {
    "type": "function",
    "function": {
        "name": "get_news",
        "description": "네이버 실시간 뉴스 조회. 특정 종목·기업·시장 뉴스가 필요할 때 사용.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "검색어. 예: '엔비디아', '코스피', '반도체'"}
            },
            "required": ["query"]
        }
    }
}

_PORTFOLIO_TOOL = {
    "type": "function",
    "function": {
        "name": "query_portfolio",
        "description": "모의투자 포트폴리오 현황 조회. 현재 보유종목, 잔고, 평가손익 등 지금 상태를 물어볼 때 사용. 과거 거래 이력은 query_trade_history 사용.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "조회 유형. 예: '현황', '잔고', '거래내역', '손익'"}
            },
            "required": ["query"]
        }
    }
}

_RAG_TOOL = {
    "type": "function",
    "function": {
        "name": "query_trade_history",
        "description": "과거 매매 이력 조회. '최근 매매 내역', '거래 기록', '언제 샀어', '매수/매도 시점', '거래 내역 보여줘' 등 과거 거래 기록을 물어볼 때 사용.",
        "parameters": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "종목코드 또는 종목명. 예: '005930', '삼성전자'"},
                "limit":  {"type": "integer", "description": "조회할 최근 거래 수 (기본 10)"}
            },
            "required": ["ticker"]
        }
    }
}

_DEEP_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "deep_search",
        "description": "AI 심층 검색 (Perplexity 스타일). 복잡한 질문, 다각도 분석, 배경 설명이 필요할 때 사용. web_search보다 느리지만 훨씬 상세한 답변.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "검색 질문 (구체적일수록 좋음)"}
            },
            "required": ["query"]
        }
    }
}

_FETCH_URL_TOOL = {
    "type": "function",
    "function": {
        "name": "fetch_url",
        "description": "특정 URL의 웹페이지 내용을 가져와 요약. 기사 링크나 공식 문서 URL을 직접 읽을 때 사용.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "읽을 웹페이지 URL (https://...)"}
            },
            "required": ["url"]
        }
    }
}

_LOCAL_KNOWLEDGE_TOOL = {
    "type": "function",
    "function": {
        "name": "search_local_knowledge",
        "description": "서버 로컬 데이터 검색. '시장 보고서', '저장된 뉴스', '서버에 있는', 'DB 뉴스', '로컬 데이터' 등을 언급할 때 반드시 사용. Oracle DB 뉴스 + 시장 분석 보고서 조회.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "검색 키워드. 예: '코스피 전망', '오늘 뉴스'"}
            },
            "required": ["query"]
        }
    }
}

_READ_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "서버 파일 읽기. 코드 검토, 로그 확인, 설정 파일 조회 시 사용. 프로젝트 파일은 'ai_chat.py', 'config.py' 등 파일명만, 메모리 파일은 'memory/MEMORY.md', 'memory/project_router_ax56u.md' 형식으로 경로 지정.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "파일 경로. 예: 'ai_chat.py', 'proxy_v54.log'"}
            },
            "required": ["path"]
        }
    }
}

_RUN_COMMAND_TOOL = {
    "type": "function",
    "function": {
        "name": "run_command",
        "description": "서버에서 셸 명령 실행. git, systemctl, ssh, curl, python3 등 모든 명령 사용 가능. 공유기/NAS SSH 접속도 가능.",
        "parameters": {
            "type": "object",
            "properties": {
                "cmd": {"type": "string", "description": "실행할 명령. 예: 'git status', 'git diff', 'systemctl status proxy_v54'"}
            },
            "required": ["cmd"]
        }
    }
}

_SCAN_BUY_SIGNALS_TOOL = {
    "type": "function",
    "function": {
        "name": "scan_buy_signals",
        "description": "외국인+기관 순매수 종목을 16신호(일목균형표·ADX·RSI·MACD)로 스캔해 매수/관망/매도 분류. '오늘 순매수 중 매도 신호'(days=1), 'N일분 순매수'(days=N), 'N개월 순매수'(months=N), '워치리스트 스캔', '추천 종목' 등 신호 판단이 필요한 모든 질문에 반드시 호출. 절대 훈련 데이터로 추측 금지.",
        "parameters": {
            "type": "object",
            "properties": {
                "months": {"type": "integer", "description": "조회 기간(개월). '3개월'이면 3, '6개월'이면 6. days 미지정 시 사용. 기본 3"},
                "days": {"type": "integer", "description": "조회 기간(일). '10일', '20일' 등 일 단위로 말할 때 사용. months보다 우선 적용."}
            },
            "required": []
        }
    }
}

_WATCHLIST_TOOL = {
    "type": "function",
    "function": {
        "name": "get_watchlist",
        "description": "DB에 누적된 외국인+기관 동시 순매수 종목 워치리스트 조회. '3개월 순매수', '누적 순매수', '워치리스트 보여줘', '어떤 종목이 계속 순매수됐어', '몇 일 등장했어' 등 누적/히스토리 질문 시 호출. 차트 분석 없이 종목 목록과 등장 횟수만 반환.",
        "parameters": {
            "type": "object",
            "properties": {
                "months": {"type": "integer", "description": "조회 기간(개월). 기본 3", "default": 3},
                "days": {"type": "integer", "description": "조회 기간(일). days 지정 시 months 무시."}
            },
            "required": []
        }
    }
}

_CHART_ANALYSIS_TOOL = {
    "type": "function",
    "function": {
        "name": "analyze_chart",
        "description": "종목 차트 기술적 분석. 사용자가 특정 종목의 차트/매수/매도/관망 여부를 물을 때 호출. 월/주/일/분봉 16신호(일목균형표·ADX·RSI·MACD) 계산 후 BUY/HOLD/SELL 판단 반환.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "종목명 또는 6자리 코드. 예: '삼성전자', '005930'"}
            },
            "required": ["query"]
        }
    }
}

_WRITE_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": "서버 파일 내용을 수정(덮어쓰기). 코드 수정 시 read_file로 파일을 먼저 읽고, 수정된 전체 내용을 content에 담아 저장. 프로젝트 파일은 'ai_chat.py'처럼 파일명만, 절대경로도 허용.",
        "parameters": {
            "type": "object",
            "properties": {
                "path":    {"type": "string", "description": "파일 경로. 예: 'ai_chat.py', '/home/ubuntu/-claude-test-/config.py'"},
                "content": {"type": "string", "description": "저장할 파일 전체 내용"}
            },
            "required": ["path", "content"]
        }
    }
}

_GIT_COMMIT_TOOL = {
    "type": "function",
    "function": {
        "name": "git_commit",
        "description": "변경된 파일을 git에 커밋. 반드시 사용자가 명시적으로 커밋을 요청했을 때만 사용.",
        "parameters": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "커밋 메시지"},
                "files":   {"type": "string", "description": "스테이징할 파일 목록 (공백 구분). 예: 'ai_chat.py llm_client.py'"}
            },
            "required": ["message"]
        }
    }
}

_ALL_TOOLS = [
    _WEB_SEARCH_TOOL, _DEEP_SEARCH_TOOL, _FETCH_URL_TOOL,
    _STOCK_PRICE_TOOL, _NEWS_TOOL, _LOCAL_KNOWLEDGE_TOOL,
    _PORTFOLIO_TOOL, _RAG_TOOL,
    _SCAN_BUY_SIGNALS_TOOL, _WATCHLIST_TOOL, _CHART_ANALYSIS_TOOL,
    _READ_FILE_TOOL, _WRITE_FILE_TOOL, _RUN_COMMAND_TOOL, _GIT_COMMIT_TOOL,
]

_TOOL_SYSTEM = """당신은 한국어 기반의 주식/시장 분석 AI 어시스턴트다.
사용자의 질문 의도를 먼저 분류한 뒤, 아래 규칙에 따라 적절한 도구를 선택한다.

[기본 원칙]
- 모델명은 "Google Gemma 4"다. GPT/Claude가 아니다.
- 항상 한국어로 답변한다.
- 사용자가 요청한 작업에 직접 필요한 도구만 사용한다.
- 도구 결과가 없거나 불충분하면, 추측하지 말고 부족한 정보를 명확히 말한다.
- 여러 도구가 필요하면, 가장 핵심 도구를 먼저 호출한 뒤 필요한 범위만 추가 호출한다.
- 시세/뉴스/보유종목/거래내역/차트분석/워치리스트를 혼동하지 않는다.
- 사용자가 종목명만 짧게 입력한 경우, 문맥상 가장 가능성 높은 의도를 추론하되 애매하면 확인 질문을 한다.
- 파일 경로나 명령 실행이 필요한 경우 서버 기본경로를 기준으로 처리한다.

[도구 라우팅 규칙]
1. 주가, 현재가, 시세, 등락률, 기간 수익률 조회
- 한국 주식이면 get_stock_price를 사용한다. 데이터 소스는 Naver 기준으로 본다.
- 해외 주식이면 get_stock_price를 사용한다. 데이터 소스는 yfinance 기준으로 본다.

2. 뉴스, 공시성 이슈, 시장 동향, 업황, 거시 이슈
- web_search를 사용한다.

3. 저장된 뉴스, 내부 보고서, 로컬 시장자료 검색
- search_local_knowledge를 사용한다.

4. 내 보유종목, 평가손익, 잔고, 계좌 상태
- query_portfolio를 사용한다.
- 계좌는 2개다: 🔵 트레이너 44197559 (가상매매), 🟡 KY 실전계좌 44384407 (실전매매).
- query_portfolio 호출 시 두 계좌 모두 함께 반환된다.

5. 매수/매도 체결내역, 기간별 거래기록, 최근 거래내역
- query_trade_history를 사용한다.
- 두 계좌(트레이너/KY) 거래내역이 함께 반환된다.

6. 차트 패턴, 지지/저항, 추세, 거래량 기반 해석
- analyze_chart를 사용한다.

7. 워치리스트 조회
- get_watchlist를 사용한다.

8. 외국인/기관 순매수, 수급 확인
- get_foreign_net_buy를 사용한다.

9. 파일 읽기/저장/쉘 명령 실행
- read_file / write_file / run_command를 사용한다.
- 서버 기본경로는 /home/ubuntu/-claude-test-/ 이다.

10. 매수신호 스캔, 종목 신호 분석
- scan_buy_signals를 사용한다. 기간 미지정 시 months=3 적용.

11. URL 직접 읽기
- fetch_url을 사용한다.

12. 심층 분석, 다각도 조사 (개념 설명, 비교 분석, 배경 조사 등 복합 질문)
- deep_search를 사용한다.
- 단, 현재 상영/개봉/출시 중인 국내 영화·드라마·콘텐츠의 단순 정보 조회는 web_search를 사용한다.

13. 특정 종목 뉴스 조회 (삼성전자, 엔비디아 등 종목명 명시된 경우)
- get_news를 사용한다. 시장 전반/업종 뉴스는 web_search를 사용한다.

[의도 분류 예시]
- "삼성전자 지금 얼마야?" → get_stock_price
- "테슬라 최근 뉴스 요약해줘" → web_search
- "지난주에 내가 뭐 샀지?" → query_trade_history
- "내 계좌 수익률 보여줘" → query_portfolio
- "엔비디아 차트 분석해줘" → analyze_chart
- "외국인이 오늘 많이 산 종목 뭐야?" → get_foreign_net_buy
- "저장된 리포트에서 반도체 전망 찾아줘" → search_local_knowledge

[복합 질의 처리]
- 질문이 복합적이면 하위 작업으로 나눈다.
예:
"삼성전자 오늘 주가랑 뉴스, 외국인 수급까지 봐줘"
→ get_stock_price + web_search + get_foreign_net_buy

- "내 보유종목 중 오늘 많이 빠진 종목 뉴스 찾아줘"
→ query_portfolio 후, 필요한 종목에 대해 web_search

[응답 원칙]
- 먼저 핵심 답을 짧게 제시하고, 필요하면 세부 내용을 덧붙인다.
- 숫자 데이터는 보기 쉽게 정리한다.
- 뉴스는 중복 내용을 합쳐 요약한다.
- 차트 분석은 단정적으로 예측하지 말고, 시나리오 형태로 설명한다.
- 투자 판단을 강요하지 말고 참고 정보로 제시한다.

[예외 처리]
- 종목 시장 구분(국내/해외)이 불명확하면 먼저 확인하거나, 문맥상 명확한 경우에만 판단한다.
- 사용자가 "이 파일 열어봐", "저장해", "명령 실행해"라고 하면 파일/명령 도구를 우선 고려한다.
- 로컬 지식과 웹 뉴스가 모두 가능할 때:
  - 저장된 자료 요청이면 search_local_knowledge 우선
  - 최신 이슈 요청이면 web_search 우선

[금지 사항]
- 도구 없이 시세를 지어내지 않는다.
- 보유종목이나 거래내역을 추정하지 않는다.
- 차트분석 결과를 확정적 투자 조언처럼 말하지 않는다.
- 파일 수정/명령 실행 시 경로를 임의로 바꾸지 않는다."""


def _execute_tool_call(tool_name: str, arguments: dict) -> str:
    """Ollama가 호출한 도구를 실행하고 결과를 반환."""
    # 지연 import (순환 참조 방지)
    from search_utils import searxng_search, naver_search
    from stock_data import stock_price_overseas, korea_invest_stock, naver_news

    query = arguments.get("query", "")
    if tool_name == "web_search":
        logger.info("Ollama tool call: web_search('%s')", query)
        results = searxng_search(query, categories="news", max_results=5, time_range="day")
        if not results:
            results = searxng_search(query, categories="news", max_results=5, time_range="week")
        # 네이버 결과 병합 (중복 URL 제거)
        naver_results = naver_search(query, max_results=3)
        seen_urls = {r.get("url") for r in results}
        for nr in naver_results:
            if nr.get("url") not in seen_urls:
                results.append(nr)
                seen_urls.add(nr.get("url"))
        if results:
            lines = []
            for r in results[:7]:
                title = r.get("title", "")
                content = r.get("content", "")[:150]
                if content:
                    lines.append(f"- {title}: {content}")
            return "\n".join(lines) if lines else "검색 결과 없음"
        return "검색 결과 없음"
    if tool_name == "deep_search":
        logger.info("Ollama tool call: deep_search('%s')", query)
        from search_utils import perplexica_search
        result = perplexica_search(query)
        return result or "심층 검색 실패"
    if tool_name == "fetch_url":
        url = arguments.get("url", query)
        logger.info("Ollama tool call: fetch_url('%s')", url)
        try:
            import requests as _req
            from html.parser import HTMLParser
            resp = _req.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
            resp.encoding = resp.apparent_encoding or "utf-8"
            class _P(HTMLParser):
                def __init__(self):
                    super().__init__()
                    self.text = []
                    self._skip = False
                def handle_starttag(self, t, _a):
                    if t in ("script", "style", "nav", "header", "footer"): self._skip = True
                def handle_endtag(self, t):
                    if t in ("script", "style", "nav", "header", "footer"): self._skip = False
                def handle_data(self, d):
                    if not self._skip and d.strip(): self.text.append(d.strip())
            p = _P(); p.feed(resp.text)
            text = " ".join(p.text)[:3000]
            return f"[{url}]\n{text}" if text else "페이지 내용을 가져올 수 없습니다."
        except Exception as e:
            return f"URL 조회 실패: {e}"
    if tool_name == "scan_buy_signals":
        months = int(arguments.get("months", 3))
        days = int(arguments["days"]) if "days" in arguments else None
        logger.info("Ollama tool call: scan_buy_signals(months=%d, days=%s)", months, days)
        from auto_trader import scan_buy_signals_for_chat
        return scan_buy_signals_for_chat(months=months, days=days)

    if tool_name == "get_watchlist":
        months = int(arguments.get("months", 3))
        days = int(arguments["days"]) if "days" in arguments else None
        logger.info("Ollama tool call: get_watchlist(months=%d, days=%s)", months, days)
        from auto_trader import get_watchlist_from_db
        rows = get_watchlist_from_db(months=months, days=days)
        period_label = f"{days}일" if days is not None else f"{months}개월"
        if not rows:
            return "DB에 누적 데이터가 없습니다. 데이터 수집 후 다시 시도하세요."
        lines = [f"📋 외국인/기관 순매수 누적 워치리스트 (최근 {period_label})\n"]
        for row in rows:
            code, name, day_cnt, both = row
            star = "⭐" if both else "  "
            lines.append(f"{star}{name}({code}) — {day_cnt}일 등장")
        return "\n".join(lines)

    if tool_name == "analyze_chart":
        logger.info("Ollama tool call: analyze_chart('%s')", query)
        from auto_trader import analyze_chart_for_chat
        return analyze_chart_for_chat(query)

    if tool_name == "get_stock_price":
        logger.info("Ollama tool call: get_stock_price('%s')", query)
        result = stock_price_overseas(query)
        if not result:
            result = korea_invest_stock(query)
        return result or f"'{query}' 주가 조회 실패"
    if tool_name == "get_news":
        logger.info("Ollama tool call: get_news('%s')", query)
        result = naver_news(query)
        return result or "뉴스 조회 실패"
    if tool_name == "search_local_knowledge":
        logger.info("Ollama tool call: search_local_knowledge('%s')", query)
        parts = []
        # RAG 벡터 검색 (의미 기반)
        try:
            from rag_store import search_memory
            rag_result = search_memory(query, n_results=5)
            if rag_result:
                parts.append(f"[RAG 기억 검색 결과]\n{rag_result}\n[RAG_END]")
        except Exception:
            pass
        # 1) 시장 보고서 (키워드 포함 시 우선)
        rpt_path = "/home/ubuntu/.openclaw/workspace-research/data/market_report.txt"
        if os.path.exists(rpt_path):
            with open(rpt_path, "r", encoding="utf-8") as f:
                rpt = f.read()
            if not query or any(k in rpt for k in query.split()):
                parts.append(f"[시장 보고서]\n{rpt[:2000]}\n[REPORT_END]")
        # 2) Oracle DB 뉴스 — 카테고리 필터링 + 키워드 검색 (Top-3)
        try:
            from db_utils import get_db_pool
            pool = get_db_pool()
            if pool:
                with pool.acquire() as conn:
                    with conn.cursor() as cur:
                        # 경제/시장/기술 뉴스만 (trade_log, 무관 항목 제외)
                        # 무관 키워드: 4월, 산, 날씨, 행사, 관광, 스포츠 등
                        cur.execute("""
                            SELECT headlines, run_time, category FROM daily_news
                            WHERE category IN ('ECONOMIC', 'MARKET', 'TECH', 'COMPANY')
                            AND (LOWER(headlines) LIKE LOWER(:kw) OR :kw IS NULL)
                            AND INSTR(UPPER(headlines), '4월') = 0
                            AND INSTR(UPPER(headlines), '산') = 0
                            AND INSTR(UPPER(headlines), '날씨') = 0
                            AND INSTR(UPPER(headlines), '행사') = 0
                            AND INSTR(UPPER(headlines), '관광') = 0
                            AND INSTR(UPPER(headlines), '스포츠') = 0
                            AND INSTR(UPPER(headlines), '매수 신호 개수') = 0
                            AND INSTR(UPPER(headlines), '매도 신호 개수') = 0
                            ORDER BY run_time DESC
                            FETCH FIRST 3 ROWS ONLY
                        """, {"kw": f"%{query}%" if query.strip() else None})
                        rows = cur.fetchall()
                        if rows:
                            for i, r in enumerate(rows, 1):
                                headline, run_time, category = r
                                # CLOB 처리: str() 변환으로 LOB 객체를 문자열로 변환
                                headline_str = str(headline) if headline else ""
                                parts.append(f"[{category}] {str(run_time)[:10]}\n{headline_str[:800]}\n[NEWS_END_{i}]")
                        else:
                            # 폴백: 최신 경제/시장 뉴스 1건
                            cur.execute("""
                                SELECT headlines, run_time FROM daily_news
                                WHERE category IN ('ECONOMIC', 'MARKET')
                                ORDER BY run_time DESC
                                FETCH FIRST 1 ROWS ONLY
                            """)
                            row = cur.fetchone()
                            if row and len(row) >= 2:  # None 체크 + 튜플 길이 확인
                                headline_str = str(row[0]) if row[0] else ""
                                run_time_str = str(row[1]) if row[1] else ""
                                parts.append(f"[최신 뉴스] {run_time_str[:10]}\n{headline_str[:1500]}\n[NEWS_END]")
        except Exception:
            logger.exception("DB 뉴스 조회 실패")
            pass
        return "\n\n".join(parts) if parts else f"로컬 DB에 '{query}' 관련 저장된 데이터 없음"
    if tool_name == "query_portfolio":
        logger.info("Ollama tool call: query_portfolio('%s')", query)
        import sqlite3 as _sq3

        def _query_db(db_path, label):
            lines = [f"[{label}]"]
            try:
                with _sq3.connect(db_path) as con:
                    row = con.execute("SELECT value FROM account WHERE key='cash'").fetchone()
                    cash = int(float(row[0])) if row else 0
                    lines.append(f"💰 현금잔고: {cash:,}원")
                    holdings = con.execute(
                        "SELECT name, ticker, qty, avg_price FROM portfolio WHERE qty > 0"
                    ).fetchall()
                    if holdings:
                        lines.append("📈 보유종목:")
                        for name, ticker, qty, avg in holdings:
                            lines.append(f"  {name}({ticker}): {qty}주 @ 평단{int(avg):,}원")
                    else:
                        lines.append("📭 보유종목 없음")
                    cols = [r[1] for r in con.execute("PRAGMA table_info(trades)").fetchall()]
                    pnl_col = ", pnl" if "pnl" in cols else ""
                    recent = con.execute(
                        f"SELECT action, name, ticker, price, qty{pnl_col}, created_at "
                        "FROM trades ORDER BY id DESC LIMIT 5"
                    ).fetchall()
                    if recent:
                        lines.append("📋 최근 거래 (5건):")
                        for r in recent:
                            if pnl_col:
                                action, name, ticker, price, qty, pnl, ts = r
                            else:
                                action, name, ticker, price, qty, ts = r
                                pnl = None
                            pnl_str = f" | 손익 {pnl:+.1f}%" if pnl is not None else ""
                            lines.append(f"  [{ts[:10]}] {action} {name}({ticker}) {qty}주 @{int(price):,}원{pnl_str}")
            except Exception as e:
                lines.append(f"조회 오류: {e}")
            return "\n".join(lines)

        base = os.path.dirname(__file__)
        result = _query_db(os.path.join(base, "mock_trading", "portfolio.db"), "🔵 트레이너 44197559")
        result += "\n\n" + _query_db(os.path.join(base, "mock_trading", "portfolio_ky.db"), "🟡 KY 실전계좌 44384407")
        return result
    if tool_name == "query_trade_history":
        ticker = arguments.get("ticker", query)
        limit  = int(arguments.get("limit", 10))
        logger.info("Ollama tool call: query_trade_history('%s', limit=%d)", ticker, limit)
        import sqlite3 as _sq3

        def _query_trades(db_path, label):
            try:
                with _sq3.connect(db_path) as con:
                    cols = [r[1] for r in con.execute("PRAGMA table_info(trades)").fetchall()]
                    extra = ", ".join(c for c in ["buy_signals", "rsi", "pnl"] if c in cols)
                    sel = "action, price, qty, created_at" + (f", {extra}" if extra else "")
                    rows = con.execute(
                        f"SELECT {sel} FROM trades WHERE ticker=? OR name LIKE ? "
                        "ORDER BY id DESC LIMIT ?",
                        [ticker, f"%{ticker}%", limit]
                    ).fetchall()
                if not rows:
                    return f"[{label}] '{ticker}' 거래 내역 없음"
                extra_cols = [c for c in ["buy_signals", "rsi", "pnl"] if c in cols]
                lines = [f"[{label}] 📚 {ticker} 매매 이력 ({len(rows)}건):"]
                for row in rows:
                    action, price, qty, ts = row[0], row[1], row[2], row[3]
                    extras = {extra_cols[i]: row[4+i] for i in range(len(extra_cols))}
                    pnl_str = f" | 손익 {extras['pnl']:+.1f}%" if extras.get("pnl") is not None else ""
                    sig_str = f" | 신호 {extras['buy_signals']}/12" if extras.get("buy_signals") is not None else ""
                    rsi_str = f" | RSI {extras['rsi']:.1f}" if extras.get("rsi") is not None else ""
                    lines.append(f"  [{ts[:10]}] {action} {qty}주 @{int(price):,}원{pnl_str}{sig_str}{rsi_str}")
                return "\n".join(lines)
            except Exception as e:
                return f"[{label}] 거래 이력 조회 오류: {e}"

        base = os.path.dirname(__file__)
        r1 = _query_trades(os.path.join(base, "mock_trading", "portfolio.db"), "🔵 트레이너 44197559")
        r2 = _query_trades(os.path.join(base, "mock_trading", "portfolio_ky.db"), "🟡 KY 실전계좌 44384407")
        return r1 + "\n\n" + r2
    if tool_name == "read_file":
        path = arguments.get("path", "")
        logger.info("Ollama tool call: read_file('%s')", path)
        ALLOWED_BASES = (
            "/home/ubuntu/-claude-test-",
            "/home/ubuntu/.claude/projects/-home-ubuntu--claude-test-/memory",
        )
        MEM_BASE = "/home/ubuntu/.claude/projects/-home-ubuntu--claude-test-/memory"
        PROJ_BASE = "/home/ubuntu/-claude-test-"
        if path.startswith("/"):
            full = os.path.realpath(path)
        elif path.startswith("memory/") or path == "memory":
            full = os.path.realpath(os.path.join(MEM_BASE, path[7:] if path.startswith("memory/") else ""))
        else:
            full = os.path.realpath(os.path.join(PROJ_BASE, path))
        if not any(full.startswith(b) for b in ALLOWED_BASES):
            return f"접근 거부: 허용된 디렉토리 외부 파일은 읽을 수 없습니다."
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as f:
                content = f.read(8000)
            lines = content.splitlines()
            if len(lines) > 200:
                content = "\n".join(lines[:200]) + f"\n... (총 {len(lines)}줄, 200줄까지 표시)"
            return f"[{path}]\n{content}"
        except FileNotFoundError:
            return f"파일 없음: {path}"
        except Exception as e:
            return f"파일 읽기 오류: {e}"

    if tool_name == "write_file":
        PROJ_BASE = "/home/ubuntu/-claude-test-"
        path = arguments.get("path", "")
        content = arguments.get("content", "")
        if not path:
            return "path가 필요합니다."
        full = os.path.realpath(path if path.startswith("/") else os.path.join(PROJ_BASE, path))
        if not full.startswith(PROJ_BASE):
            return "접근 거부: 프로젝트 디렉토리 외부는 쓸 수 없습니다."
        try:
            if os.path.exists(full):
                import shutil
                shutil.copy2(full, full + ".bak")
            with open(full, "w", encoding="utf-8") as f:
                f.write(content)
            logger.info("Ollama tool call: write_file('%s', %d bytes)", path, len(content))
            return f"저장 완료: {path} ({len(content)} bytes)"
        except Exception as e:
            return f"파일 쓰기 오류: {e}"

    if tool_name == "run_command":
        import subprocess, shlex
        cmd = arguments.get("cmd") or arguments.get("command") or arguments.get("shell_command") or ""
        logger.info("Ollama tool call: run_command('%s')", cmd)
        try:
            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True,
                timeout=60, cwd="/home/ubuntu/-claude-test-"
            )
            out = (result.stdout + result.stderr).strip()
            return out[:3000] if out else "(출력 없음)"
        except subprocess.TimeoutExpired:
            return "명령 타임아웃 (30초 초과)"
        except Exception as e:
            return f"명령 실행 오류: {e}"

    if tool_name == "git_commit":
        import subprocess
        message = arguments.get("message", "")
        files = arguments.get("files", "")
        logger.info("Ollama tool call: git_commit('%s', files='%s')", message, files)
        if not message:
            return "커밋 메시지가 없습니다."
        try:
            cwd = "/home/ubuntu/-claude-test-"
            # 스테이징
            if files:
                for f in files.split():
                    subprocess.run(["git", "add", f], cwd=cwd, capture_output=True)
            else:
                subprocess.run(["git", "add", "-u"], cwd=cwd, capture_output=True)
            # 커밋
            result = subprocess.run(
                ["git", "commit", "-m", message],
                cwd=cwd, capture_output=True, text=True, timeout=30
            )
            out = (result.stdout + result.stderr).strip()
            return out if out else "커밋 완료"
        except Exception as e:
            return f"커밋 오류: {e}"

    return f"알 수 없는 도구: {tool_name}"


_GEMMA3_TOOL_SYSTEM = """나는 한국어 AI 어시스턴트입니다. 사용자와의 대화에서 도구가 필요하면 도구를 호출하여 검증된 최신 실시간 데이터를 기반으로 대화를 만들어 냅니다. 절대로 수치를 추측하거나 만들지 마세요.

도구 호출 형식 — JSON 한 줄만, 다른 텍스트 없이:
{"tool":"도구명","arguments":{"query":"검색어"}}

[접근 가능한 데이터]
- Oracle DB: daily_news 테이블 (매일 수집된 뉴스 헤드라인) → search_local_knowledge
- 시장 보고서: 매일 갱신되는 코스피/코스닥 분석 텍스트 → search_local_knowledge
- SQLite portfolio.db: 모의투자 잔고·보유종목·거래내역 → query_portfolio
사용자가 "DB", "저장된", "서버", "로컬" 등을 언급하면 반드시 도구로 조회할 것.

사용 가능한 도구:
- get_stock_price: 주가·시세 조회 (종목명 또는 티커)
- get_news: 종목·기업·시장 뉴스
- web_search: 최신 정보·뉴스 검색
- search_local_knowledge: 시장보고서·DB뉴스·RAG 조회
- query_portfolio: 잔고·보유종목·거래내역
- query_trade_history: 특정 종목 과거 매매 이력
- deep_search: 복잡한 심층 분석
- fetch_url: 특정 URL 읽기

[예시]
사용자: 삼성전자 주가
{"tool":"get_stock_price","arguments":{"query":"삼성전자"}}

사용자: 애플 주가 조회해줘
{"tool":"get_stock_price","arguments":{"query":"AAPL"}}

사용자: 오늘 코스피 시황은?
{"tool":"get_news","arguments":{"query":"코스피 시황"}}

사용자: 내 포트폴리오 보여줘
{"tool":"query_portfolio","arguments":{"query":"현황"}}

사용자: 왕과 사는 남자 줄거리 요약
{"tool":"web_search","arguments":{"query":"왕과 사는 남자 영화 줄거리"}}

사용자: 안녕
안녕하세요! 무엇을 도와드릴까요?"""


def call_gemma3(prompt: str, use_tools: bool = True) -> str:
    """gemma3:4b 로컬 호출. 커스텀 tool calling (프롬프트 기반) 지원."""
    import datetime as _dt, pytz as _pytz, json as _json, re as _re
    _now = _dt.datetime.now(_pytz.timezone("Asia/Seoul"))
    # 날짜를 유저 메시지 앞에 붙임 → 시스템 프롬프트 고정 → Ollama KV 캐시 재사용
    _DAYS_KO = ["월요일","화요일","수요일","목요일","금요일","토요일","일요일"]
    _dated_prompt = f"[{_now.strftime('%Y-%m-%d')} {_DAYS_KO[_now.weekday()]} {_now.strftime('%H:%M KST')}] {prompt}"
    messages = [
        {"role": "system", "content": _GEMMA3_TOOL_SYSTEM},
        {"role": "user",   "content": _dated_prompt},
    ]
    _tool_called = False  # 도구는 1회만 허용 (연쇄 호출 방지)
    for attempt in range(3):
        try:
            r = requests.post(
                config.LOCAL_OLLAMA_URL,
                json={
                    "model": "gemma3:4b",
                    "messages": messages,
                    "options": {"temperature": 0.7, "num_predict": 600, "num_ctx": 1024, "num_thread": 4},
                    "stream": False,
                },
                timeout=(5, 120),
                proxies={"http": None, "https": None},
            )
            r.raise_for_status()
            content = _clean_llm_output(r.json().get("message", {}).get("content", "").strip())
            if not content:
                continue
            if not use_tools or _tool_called:
                return content
            # tool call 감지: 중첩 JSON 브레이스 카운팅으로 정확히 추출
            tool_data = None
            idx = content.find('{"tool"')
            if idx >= 0:
                depth, end = 0, idx
                for i, ch in enumerate(content[idx:]):
                    if ch == '{': depth += 1
                    elif ch == '}':
                        depth -= 1
                        if depth == 0:
                            end = idx + i + 1
                            break
                try:
                    tool_data = _json.loads(content[idx:end])
                except Exception:
                    pass
            if tool_data is None:
                try:
                    tool_data = _json.loads(content)
                except Exception:
                    pass
            if tool_data and "tool" in tool_data:
                tool_name = tool_data["tool"]
                args = tool_data.get("arguments", {})
                args = {k: (v["value"] if isinstance(v, dict) and "value" in v else v)
                        for k, v in args.items()}
                logger.info("Gemma3 tool call: %s(%s)", tool_name, args)
                tool_result = _execute_tool_call(tool_name, args)
                _tool_called = True
                # 요약 호출: 시스템 프롬프트를 단순화해서 도구 재호출 방지
                messages[0] = {"role": "system", "content": "한국어로 간결하게 답변하는 AI입니다. JSON이나 도구 호출 없이 텍스트로만 답하세요."}
                messages.append({"role": "assistant", "content": content})
                messages.append({"role": "user", "content": f"[검색 결과]\n{tool_result}\n\n위 내용을 바탕으로 한국어로 간결하게 답해줘."})
                continue
            return content
        except Exception as e:
            logger.error("Gemma3 호출 실패: %s", e)
            time.sleep(2)
    return "⚠️ 서버 AI 응답 실패"


def call_mistral_vision(prompt: str, image_path: str, system: str = "한국 주식 트레이딩 전문가. 차트 이미지를 보고 구체적이고 간결하게 한국어로 분석.") -> str:
    """
    google_gemma-4-26b-a4b-it 비전 호출.
    image_path: PNG 파일 경로 → base64 인코딩 후 전송
    """
    import base64 as _b64, time as _time
    _last_ollama_request[0] = _time.time()
    send_wol()
    wait_for_ollama()

    try:
        with open(image_path, "rb") as f:
            img_b64 = _b64.b64encode(f.read()).decode("utf-8")
    except Exception as e:
        logger.error("차트 이미지 읽기 실패: %s", e)
        return ""

    import datetime as _dt, pytz as _pytz
    _now = _dt.datetime.now(_pytz.timezone("Asia/Seoul"))
    _DAYS_KO = ["월요일","화요일","수요일","목요일","금요일","토요일","일요일"]
    _dated_prompt = f"[{_now.strftime('%Y-%m-%d')} {_DAYS_KO[_now.weekday()]} {_now.strftime('%H:%M KST')}] {prompt}"

    # OpenAI 호환 API 페이로드 (LM Studio localhost:8000)
    payload = {
        "model": "google_gemma-4-26b-a4b-it",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": _dated_prompt, "images": [img_b64]},
        ],
        "temperature": 0.5,
        "max_tokens": 8000,
    }
    try:
        r = requests.post(
            "http://221.144.111.116:8000/v1/chat/completions",
            headers=_LM_HEADERS,
            json=payload,
            timeout=120,
            proxies={"http": None, "https": None},
        )
        r.raise_for_status()
        data = r.json()
        return _clean_llm_output(((data.get("choices") or [{}])[0].get("message") or {}).get("content", "").strip())
    except Exception as e:
        logger.error("call_mistral_vision 실패: %s", e)
        return ""


def call_mistral_only(prompt: str, system: str = _TOOL_SYSTEM, use_tools: bool = True, history_messages: list = None) -> str:
    """
    google_gemma-4-26b-a4b-it (LM Studio) 단독 호출.
    - use_tools=True: 2단계 RAG 텍스트 주입 방식 도구 호출
      1단계) tool_memory에서 관련 도구 정의 검색 → [사용 가능한 도구] 블록으로 주입 (read-only)
      2단계) news/scan/trade_memory에서 컨텍스트 검색 → [참고 데이터] 블록으로 주입 (read+write)
    - history_messages: [{"role": "user", ...}, {"role": "assistant", ...}] 형식
    - 3회 재시도 후 최종 실패 시 안내 메시지 반환.
    """
    import time as _time, json as _json, re as _re
    _last_ollama_request[0] = _time.time()
    send_wol()
    _wol_waited = False

    import datetime as _dt, pytz as _pytz
    _now = _dt.datetime.now(_pytz.timezone("Asia/Seoul"))
    _DAYS_KO = ["월요일","화요일","수요일","목요일","금요일","토요일","일요일"]
    _dated_prompt = f"[{_now.strftime('%Y-%m-%d')} {_DAYS_KO[_now.weekday()]} {_now.strftime('%H:%M KST')}] {prompt}"

    # ── 2단계 RAG 주입 ──────────────────────────────────────────────
    _system = system
    if use_tools:
        try:
            from rag_store import search_tools as _st, search_memory as _sm
            # 1단계: 관련 도구 정의 검색 (read-only) — 도구 수가 적으므로 전체 반환
            _tool_text = _st(prompt, n_results=20)
            if _tool_text:
                _system += (
                    "\n\n[사용 가능한 도구]\n"
                    "도구 호출이 필요하면 다음 JSON 형식으로만 응답하라 (다른 텍스트 없이):\n"
                    '{"tool":"도구명","arguments":{"파라미터":"값"}}\n\n'
                    + _tool_text
                )
            # 2단계: 컨텍스트 메모리 검색 (read+write — scan/news/trade 포함)
            _ctx = _sm(prompt, n_results=3)
            if _ctx:
                _system += f"\n\n[참고 데이터]\n{_ctx}"
        except Exception as _rag_e:
            logger.warning("RAG 주입 실패: %s", _rag_e)
    # ────────────────────────────────────────────────────────────────

    messages = [{"role": "system", "content": _system}]
    if history_messages:
        messages.extend(history_messages)
    messages.append({"role": "user", "content": _dated_prompt})

    # OpenAI 호환 API 페이로드 (LM Studio localhost:8000)
    payload = {
        "model": "google_gemma-4-26b-a4b-it",
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 3000,
    }
    # RAG 텍스트 주입 방식 — native tools payload 미사용

    _DIRECT_RETURN_TOOLS = {
        "scan_buy_signals", "get_watchlist", "analyze_chart",
    }
    _KNOWN_TOOLS = {
        "scan_buy_signals", "get_watchlist", "analyze_chart", "get_stock_price",
        "get_news", "web_search", "query_portfolio", "query_trade_history",
        "get_foreign_net_buy", "search_local_knowledge", "deep_search",
        "fetch_url", "read_file", "write_file", "run_command", "git_commit",
    }

    def _parse_text_tool_call(content: str):
        """content에서 {"tool":"...", "arguments":{...}} 추출."""
        # Gemma 4 native 형식: <|tool_call>call:tool_name{key:<|"|>val<|"|>}<tool_call|>
        _gemma_m = _re.search(r'<\|tool_call>call:(\w+)\{([^}]*)\}', content)
        if _gemma_m:
            tool_name = _gemma_m.group(1)
            if tool_name in _KNOWN_TOOLS:
                args = {}
                raw_args = _gemma_m.group(2)
                # key:<|"|>value<|"|> 형식 파싱
                for _kv in _re.finditer(r'(\w+):\s*(?:<\|"\|>([^<]*)<\|"\|>|(\S+))', raw_args):
                    k = _kv.group(1)
                    v = _kv.group(2) if _kv.group(2) is not None else _kv.group(3)
                    try:
                        args[k] = int(v)
                    except (ValueError, TypeError):
                        args[k] = v or ""
                return {"tool": tool_name, "arguments": args}

        # [TOOL_CALLS]tool_name[ARGS]{...} 형식 (Ollama 비표준 형식)
        _tc_m = _re.search(r'\[TOOL_CALLS\](\w+)\[ARGS\]({.*})', content)
        if _tc_m:
            try:
                tool_name = _tc_m.group(1)
                args_json = _tc_m.group(2)
                args = _json.loads(args_json)
                if tool_name in _KNOWN_TOOLS:
                    return {"tool": tool_name, "arguments": args}
            except Exception:
                pass

        # {"tool": ...} 형식 (표준 JSON)
        idx = content.find('{"tool"')
        if idx >= 0:
            depth, end = 0, idx
            for i, ch in enumerate(content[idx:]):
                if ch == '{': depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        end = idx + i + 1
                        break
            try:
                d = _json.loads(content[idx:end])
                if isinstance(d, dict) and "tool" in d:
                    return d
            except Exception:
                pass
        # 전체가 JSON인 경우
        try:
            d = _json.loads(content.strip())
            if isinstance(d, dict) and "tool" in d:
                return d
        except Exception:
            pass
        # Python 함수 호출 형식: tool_name(key=val, ...)
        _fn_m = _re.match(r'^(\w+)\(([^)]*)\)\s*$', content.strip())
        if _fn_m and _fn_m.group(1) in _KNOWN_TOOLS:
            args = {}
            for _kv in _fn_m.group(2).split(','):
                _kv = _kv.strip()
                if '=' in _kv:
                    _k, _v = _kv.split('=', 1)
                    try:
                        args[_k.strip()] = int(_v.strip())
                    except ValueError:
                        args[_k.strip()] = _v.strip().strip("'\"")
            return {"tool": _fn_m.group(1), "arguments": args}
        return None

    last_exc = None
    for attempt in range(1, config.LLM_MAX_RETRY + 1):
        try:
            r = requests.post("http://221.144.111.116:8000/v1/chat/completions", headers=_LM_HEADERS, json=payload, timeout=(5, 300))
            r.raise_for_status()
            data = r.json()
            # OpenAI 호환 응답 형식: {"choices": [{"message": {"content": "..."}}]}
            msg = (data.get("choices") or [{}])[0].get("message", {})

            # 도구 호출 루프 (최대 3라운드)
            for _round in range(3):
                content = (msg.get("content") or "").strip()
                if not content:
                    break

                tool_data = _parse_text_tool_call(content)
                if not tool_data:
                    return _clean_llm_output(content)

                tool_name = tool_data.get("tool", "")
                args = tool_data.get("arguments", {})
                if "command" in args and "cmd" not in args:
                    args["cmd"] = args.pop("command")
                logger.info("RAG tool call [round %d]: %s(%s)", _round + 1, tool_name, args)
                tool_result = _execute_tool_call(tool_name, args)

                if tool_name in _DIRECT_RETURN_TOOLS:
                    return tool_result

                # 도구 결과 주입 후 재호출
                messages.append({"role": "assistant", "content": content})
                messages.append({"role": "user", "content": f"[도구 결과: {tool_name}]\n{tool_result}\n\n위 결과를 바탕으로 한국어로 답해줘."})
                payload2 = {
                    "model": "google_gemma-4-26b-a4b-it",
                    "messages": messages,
                    "temperature": 0.7,
                    "max_tokens": 3000,
                }
                r2 = requests.post("http://221.144.111.116:8000/v1/chat/completions", headers=_LM_HEADERS, json=payload2, timeout=(5, 300))
                r2.raise_for_status()
                msg = (r2.json().get("choices") or [{}])[0].get("message", {})
                continue

            return _clean_llm_output((msg.get("content") or "").strip()) or _parse_ollama_response(r)

        except Exception as e:
            last_exc = e
            err_str = str(e).lower()
            if not _wol_waited and any(k in err_str for k in ["connect", "refused", "timeout", "unreachable"]):
                _wol_waited = True
                logger.warning("Ollama 연결 실패 — PC 절전 의심, 응답 대기 중")
                def _notify():
                    try:
                        import telebot as _tb
                        _bot = _tb.TeleBot(config.TOKEN_RAW)
                        _bot.send_message(config.CHAT_ID, "💤 PC가 절전 상태입니다. Wake on LAN으로 깨우는 중...\n⏳ 1~2분 후 자동 재시도됩니다.")
                    except Exception:
                        pass
                threading.Thread(target=_notify, daemon=True).start()
                logger.info("Ollama 응답 대기 중 (최대 120초)...")
                if wait_for_ollama(timeout=180, interval=10):
                    continue
                else:
                    return "💤 PC가 절전 상태입니다. Wake on LAN으로 깨우는 중...\n⏳ 잠시 후 다시 말씀해 주세요. (보통 1~2분)"
            wait = 2 ** (attempt - 1)
            logger.warning("google_gemma-4-26b-a4b-it 시도 %d/%d 실패 (%s) — %ds 후 재시도",
                           attempt, config.LLM_MAX_RETRY, str(e)[:80], wait)
            if attempt < config.LLM_MAX_RETRY:
                time.sleep(wait)

    logger.error("google_gemma-4-26b-a4b-it %d회 모두 실패: %s", config.LLM_MAX_RETRY, str(last_exc)[:200])
    return "⚠️ PC LM Studio 서버 불안정. 잠시 후 다시 시도해주세요.\n모의투자(/mock)는 정상 작동 중입니다."


# 기존 call_qwen 호출부 호환성 유지
call_qwen = call_mistral_only


def _ollama_alive() -> bool:
    """LM Studio (localhost:8000) 응답 가능 여부를 확인."""
    try:
        r = requests.get("http://221.144.111.116:8000/v1/models", headers=_LM_HEADERS, timeout=3)
        return r.status_code == 200
    except Exception:
        return False
