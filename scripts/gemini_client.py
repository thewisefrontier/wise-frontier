"""
scripts/gemini_client.py
--------------------------
9개 writer 스크립트(gemini_writer.py, gemini_summarizer.py, daily_digest.py,
oil_price_writer.py, econ_writer.py, opinet_price_writer.py,
opinet_weekly_writer.py, backfill_value_add.py, verify_entities.py)에 각자
복제돼 있던 call_gemini() 캐스케이드(5단 모델 폴백 + 키 로테이션 + 타임아웃/
503/429/MAX_TOKENS 처리)를 공용화.

실사고 이력(2026-08-19~20): 이 로직이 파일마다 복제돼 있어서 한 곳에서 고친
안전장치(타임아웃 시 다음 키로 재시도, MAX_TOKENS 시 다음 모델로 폴백)가
나머지엔 반영이 안 됐다. daily_digest.py는 타임아웃 시 즉시 전체 포기가
남아있어 8/18·8/19 무발행, oil_price_writer.py를 포함한 8개 파일 전부
MAX_TOKENS 시 즉시 포기가 남아있어 국제유가가 8/13 이후 무발행됐다.
script_leak.py·json_body_guard.py와 같은 이유로 공용화한다.

각 스크립트는 자기 GEMINI_API_KEYS로 GeminiClient 인스턴스를 하나 만들고,
기존 call_gemini(prompt, max_tokens=..., start_tier=...) 시그니처를 그대로
유지하는 얇은 wrapper에서 이 인스턴스의 .call()을 호출한다 — 호출부(수십 곳)
코드는 전혀 안 바뀐다.
"""

import os
import time
from datetime import datetime, timezone, timedelta

import requests

# 자체 사용량 카운팅(2026-09-03) — "우리가 얼마나 쓰는지 자체적으로
# 카운팅이 안되나?" 요청. Google AI Studio가 더는 문서에 고정 한도 수치를
# 안 싣고 로그인된 대시보드에서만 보여줘서, 이 클라이언트가 자기 호출을
# 직접 집계해 Supabase gemini_usage_daily에 쌓는다(RPC increment_gemini_usage,
# security definer라 service_role 키만 있으면 별도 테이블 권한 없이도 동작).
# 실패해도 실제 Gemini 호출 자체는 절대 막지 않는다(집계는 부가 기능).
_USAGE_SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
_USAGE_SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
_KST = timezone(timedelta(hours=9))


def _log_usage(model: str, key_index: int, outcome: str,
               prompt_tokens: int = 0, completion_tokens: int = 0, total_tokens: int = 0) -> None:
    if not _USAGE_SUPABASE_URL or not _USAGE_SUPABASE_KEY:
        return
    try:
        requests.post(
            f"{_USAGE_SUPABASE_URL}/rest/v1/rpc/increment_gemini_usage",
            headers={
                "apikey": _USAGE_SUPABASE_KEY,
                "Authorization": f"Bearer {_USAGE_SUPABASE_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "p_date": datetime.now(_KST).date().isoformat(),
                "p_model": model,
                "p_key_index": key_index,
                "p_outcome": outcome,
                "p_prompt_tokens": prompt_tokens,
                "p_completion_tokens": completion_tokens,
                "p_total_tokens": total_tokens,
            },
            timeout=5,
        )
    except Exception:
        pass  # 집계 실패는 무시 — 본 기능(Gemini 호출)에 영향 없어야 한다


DEFAULT_GEMINI_MODELS = [
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
]

# 429/503으로 키를 바로바로 넘기면 짧은 시간에 여러 키를 몰아 쏘게 되어
# 분당 한도(RPM)를 스크립트 자신이 태워버릴 수 있다(2026-08-22 실사고 —
# frontier_markets_writer.py 수동 실행이 5키×2모델 연속 호출로 RPM 초과).
RETRY_DELAY = 2


class GeminiClient:
    def __init__(self, api_keys, models=None):
        self.api_keys = api_keys or []
        self.models = models or DEFAULT_GEMINI_MODELS
        self._current_key_idx = 0
        self._exhausted_keys = {m: set() for m in self.models}

    def call(self, prompt, max_tokens=1500, start_tier=0, temperature=0.5,
             timeout=(10, 45), use_search=False, max_stages=None):
        if not self.api_keys:
            print("[ERROR] GEMINI_API_KEY 없음")
            return None

        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
        }
        if use_search:
            payload["tools"] = [{"google_search": {}}]

        n = len(self.api_keys)
        model_stages = [(m, self._exhausted_keys[m]) for m in self.models[start_tier:]]
        # 검색 그라운딩(use_search)은 구글 쪽에서 그라운딩 전용 쿼터가 아니라
        # 훨씬 작은 generate_content_free_tier_requests(키당 하루 20건 수준)로
        # 잘못 집계되는 사례가 보고돼 있다(2026-08-22 실사고, frontier_markets_writer.py
        # 25회=5키×5모델 연속 429). 이 좁은 쿼터는 모델을 바꿔도 안 풀릴 가능성이
        # 높으므로, 모델 단계를 끝까지 도는 대신 max_stages로 시도 폭을 제한해
        # 쿼터를 헛되이 태우지 않고 빨리 포기하게 한다(호출부가 비검색 폴백으로
        # 넘어갈 수 있게).
        if max_stages is not None:
            model_stages = model_stages[:max_stages]

        for model, exhausted in model_stages:
            available = [i for i in range(n) if i not in exhausted]
            if not available:
                print(f"  [{model}] 모든 키 소진 → 다음 모델로")
                continue

            ordered = sorted(available, key=lambda i: (i - self._current_key_idx) % n)

            for idx in ordered:
                api_key = self.api_keys[idx]
                url = (
                    f"https://generativelanguage.googleapis.com/v1beta/models/"
                    f"{model}:generateContent?key={api_key}"
                )
                try:
                    res = requests.post(url, json=payload, timeout=timeout)
                    if res.status_code == 200:
                        self._current_key_idx = (idx + 1) % n
                        body = res.json()
                        usage = body.get("usageMetadata", {}) or {}
                        _log_usage(model, idx + 1, "success",
                                   usage.get("promptTokenCount", 0),
                                   usage.get("candidatesTokenCount", 0),
                                   usage.get("totalTokenCount", 0))
                        cands = body.get("candidates", [])
                        if not cands:
                            return None
                        # maxOutputTokens 초과로 잘린 응답을 정상 취급하면 문장·JSON이
                        # 중간에서 끊긴 채 저장된다(실사고 id=47879 등). 같은 키로
                        # 재시도해도 같은 모델이면 다시 잘릴 뿐이라 다음 모델로 넘어간다
                        # (해당 키는 소진 처리하지 않음 — RPD와 무관한 문제).
                        _finish = cands[0].get("finishReason", "")
                        if _finish and _finish != "STOP":
                            print(f"  [WARN] {model} 응답 비정상 종료(finishReason={_finish}) → 다음 모델로")
                            break
                        parts = cands[0].get("content", {}).get("parts", [])
                        text = "".join(p.get("text", "") for p in parts).strip()
                        return text if text else None
                    elif res.status_code == 429:
                        print(f"  [429] {model} 키 {idx+1} 한도 초과 → 다음 키")
                        _log_usage(model, idx + 1, "429")
                        exhausted.add(idx)
                        time.sleep(RETRY_DELAY)
                        continue
                    elif res.status_code == 503:
                        print(f"  [503] {model} 키 {idx+1} 과부하 → 다음 키")
                        _log_usage(model, idx + 1, "503")
                        time.sleep(RETRY_DELAY)
                        continue
                    else:
                        print(f"[ERROR] Gemini {res.status_code}: {res.text[:200]}")
                        _log_usage(model, idx + 1, "other")
                        return None
                except requests.exceptions.Timeout:
                    print(f"  [TIMEOUT] {model} 키 {idx+1} → 다음 키")
                    continue
                except Exception as e:
                    print(f"[ERROR] {e}")
                    return None

        print("[ERROR] 모든 모델/키 소진")
        return None
