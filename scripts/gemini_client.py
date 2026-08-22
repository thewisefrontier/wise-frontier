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

import time

import requests

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
             timeout=(10, 45), use_search=False):
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
                        cands = res.json().get("candidates", [])
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
                        exhausted.add(idx)
                        time.sleep(RETRY_DELAY)
                        continue
                    elif res.status_code == 503:
                        print(f"  [503] {model} 키 {idx+1} 과부하 → 다음 키")
                        time.sleep(RETRY_DELAY)
                        continue
                    else:
                        print(f"[ERROR] Gemini {res.status_code}: {res.text[:200]}")
                        return None
                except requests.exceptions.Timeout:
                    print(f"  [TIMEOUT] {model} 키 {idx+1} → 다음 키")
                    continue
                except Exception as e:
                    print(f"[ERROR] {e}")
                    return None

        print("[ERROR] 모든 모델/키 소진")
        return None
