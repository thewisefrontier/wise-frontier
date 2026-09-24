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
import base64
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

# 자체 사용량 카운팅(2026-09-03) — "우리가 얼마나 쓰는지 자체적으로
# 카운팅이 안되나?" 요청. Google AI Studio가 더는 문서에 고정 한도 수치를
# 안 싣고 로그인된 대시보드에서만 보여줘서, 이 클라이언트가 자기 호출을
# 직접 집계해 Supabase gemini_usage_daily에 쌓는다(RPC increment_gemini_usage,
# security definer라 service_role 키만 있으면 별도 테이블 권한 없이도 동작).
# 실패해도 실제 Gemini 호출 자체는 절대 막지 않는다(집계는 부가 기능).
_USAGE_SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
_USAGE_SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
# 2026-09-08 수정: RPD(하루 요청 한도)는 KST가 아니라 태평양시간(America/
# Los_Angeles) 자정 기준으로 리셋된다(공식 확인: ai.google.dev/gemini-api/docs/
# rate-limits + Google 커뮤니티 스레드 — "AI Studio not resetting... even after
# midnight Pacific Time"). KST 자정으로 집계하면 구글의 실제 리셋 시점과
# 최대 17시간(PST 기준)까지 어긋나 "오늘 잔여 한도" 계산이 실제보다 부풀거나
# 줄어들게 잘못 나온다 — 사용자가 카운터 값과 RPD 한도를 대조하다 발견.
_PACIFIC = ZoneInfo("America/Los_Angeles")


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
                "p_date": datetime.now(timezone.utc).astimezone(_PACIFIC).date().isoformat(),
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
    "gemini-3.8-flash",
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

# 2026-09-13 실사고(사용자 지적 — "2번째 키 한도를 보니까 3.1플래시라이트는
# 47개, 3.5플래시라이트는 158개만 썼네"): RPD 500인 lite 모델 키가 실제로는
# 30%도 안 쓴 상태에서 "모든 키 소진"으로 몇 시간째 기사 생성이 0건이었다.
# 원인은 429를 받으면 그게 분당 한도(RPM, 금방 풀림)든 일일 한도(RPD, 태평양
# 자정까지 안 풀림)든 구분 없이 그 키를 이 프로세스가 끝날 때까지 영구 제외
# 하던 것 — 5키가 각각 RPM 버스트로 429를 딱 한 번씩만 받아도 "가용 키 0개"가
# 돼버린다. 429 응답 본문의 quotaId로 실제 어느 한도인지 구분해, RPD가 아니면
# 짧은 쿨다운 후 같은 프로세스 안에서도 다시 쓸 수 있게 한다.
KEY_COOLDOWN_SECONDS = 65  # RPM 윈도우(60초)가 확실히 지나도록 여유를 둠

# 2026-09-14 추가(사용자 지시 — "호출 사이에 텀을 둬서 전체 처리 속도를
# 늦춰. 어쩔 수 없지."): 위 쿨다운은 이미 벌어진 429를 자동 복구시킬 뿐,
# 애초에 429가 자주 뜨는 근본 원인(한 사이클 안에서 짧은 시간에 호출이
# 몰려 개별 키의 분당 한도를 태움)은 그대로였다. 키별로 마지막 호출 후
# 최소 이 간격만큼 지나야 그 키를 다시 쓰게 강제해 분당 한도 안에 들어오게
# 한다 — 처리 속도가 느려지는 트레이드오프를 사용자가 감수하기로 함.
#
# 모델마다 실제 RPM이 달라(프리미엄 티어 RPM=5, lite 티어는 RPD 500 기준
# 그보다 훨씬 높음) 간격도 모델별로 다르게 둔다. 프리미엄은 RPM=5 → 키당
# 12초 간격(분당 5회, 딱 한도)이 아니라 여유를 둬 13초. lite는 정확한 RPM이
# 공개돼 있지 않지만 RPD 500 규모에 비춰 5초(키당 분당 12회, 5키 합산
# 최대 분당 60회)면 안전한 하한선으로 판단.
_PREMIUM_MIN_INTERVAL = 13
_LITE_MIN_INTERVAL = 5
MIN_KEY_INTERVAL_SECONDS = {
    "gemini-3.8-flash": _PREMIUM_MIN_INTERVAL,
    "gemini-3.7-flash": _PREMIUM_MIN_INTERVAL,
    "gemini-3.6-flash": _PREMIUM_MIN_INTERVAL,
    "gemini-3.5-flash": _PREMIUM_MIN_INTERVAL,
    "gemini-3.5-flash-lite": _LITE_MIN_INTERVAL,
    "gemini-3.1-flash-lite": _LITE_MIN_INTERVAL,
}


def _min_interval(model: str) -> float:
    return MIN_KEY_INTERVAL_SECONDS.get(model, _LITE_MIN_INTERVAL)


_SHARED_WAIT_CAP = 20  # 이 이상은 기다리지 않고 그냥 진행 — 다른 키/모델로
                       # 넘어가는 게 나을 수도 있는 상황을 여기서 무한정 막지 않는다.


def _wait_for_shared_slot(model: str, idx: int, min_interval: float) -> None:
    """다른 프로세스가 이 키·모델을 방금 썼으면 그만큼 기다린 뒤 우리 몫을
    예약한다. 총 대기가 _SHARED_WAIT_CAP을 넘으면 포기하고 그냥 진행한다."""
    waited = 0.0
    for _ in range(4):
        wait = _claim_key_shared(model, idx, min_interval)
        if wait <= 0:
            return
        wait = min(wait, _SHARED_WAIT_CAP - waited)
        if wait <= 0:
            return
        time.sleep(wait)
        waited += wait


# 임베딩 모델(gemini-embedding-001) 전용 페이싱 — 생성 모델과는 별도
# 쿼터 풀. 사용자가 공식 수치로 확인: RPM 100 / TPM 30k / RPD 1000
# (Gemini Embedding 1·2 공통, 키 1개 기준). RPM 100 → 키당 최소 0.6초
# 간격이면 한도에 딱 맞지만, 여유를 둬 1초(키당 분당 60회, 5키 합산
# 최대 분당 300회)로 설정 — 이 프로젝트의 "실사용량을 실측해 조정" 방침상
# _log_usage()로 쌓이는 gemini_usage_daily를 보고 필요하면 더 낮춰도 된다.
_EMBED_MIN_INTERVAL = 1

# 2026-09-24 실사고: run.yml이 collectors/breaking/heavy 3개 job으로 쪼개지며
# 서로 다른 프로세스(domestic_kr_writer.py, explainer_writer.py,
# multilang_translate.py, gemini_summarizer.py, gemini_writer.py 등)가 같은
# 5개 키를 동시에 호출하게 됐다. 위 _min_interval() 페이싱은 프로세스 메모리
# 안에서만 유지돼 서로의 존재를 몰랐다 — 각자 "안전하게" 페이싱해도 합산하면
# 분당 한도를 넘겨, 무거운 실행마다 429가 14~22회 났다(고쳐지지 않은 경우
# 이전/이후 비교해도 동일해 이 원인임을 확인). Supabase에 실제 마지막 호출
# 시각을 남겨 프로세스 경계를 넘어 공유한다(claim_gemini_key RPC). DB 호출
# 자체가 실패하면(네트워크 등) 막지 않고 기존 프로세스 내 페이싱만으로
# 진행한다 — 이 프로젝트 방침대로 부가 기능이 본 기능을 막지 않는다.
_PACING_SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
_PACING_SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")


def _claim_key_shared(model: str, idx: int, min_interval: float) -> float:
    """다른 프로세스와 공유된 페이싱을 확인한다. 호출해도 되면 0(대기 불필요),
    아니면 기다려야 할 초를 반환한다. DB 호출 실패 시 0(=막지 않음)."""
    if not _PACING_SUPABASE_URL or not _PACING_SUPABASE_KEY:
        return 0.0
    try:
        res = requests.post(
            f"{_PACING_SUPABASE_URL}/rest/v1/rpc/claim_gemini_key",
            headers={
                "apikey": _PACING_SUPABASE_KEY,
                "Authorization": f"Bearer {_PACING_SUPABASE_KEY}",
                "Content-Type": "application/json",
            },
            json={"p_model": model, "p_key_index": idx, "p_min_interval": min_interval},
            timeout=5,
        )
        if res.status_code in (200, 201):
            row = (res.json() or [{}])[0]
            return 0.0 if row.get("allowed") else float(row.get("wait_seconds") or 0)
    except Exception:
        pass
    return 0.0


def _quota_info(res) -> str:
    """429 본문에서 실제로 걸린 한도(quotaId=값). 요청 수(RPM)인지 토큰 수(TPM)인지
    구분용 — 키(프로젝트)별로 간격을 두는데도 5키가 연달아 429를 받는 원인 추적(2026-09-25)."""
    try:
        return ", ".join(f"{v.get('quotaId', '?')}={v.get('quotaValue', '?')}"
                         for d in res.json().get("error", {}).get("details", [])
                         for v in d.get("violations", []))
    except Exception:
        return ""


def _is_per_day_quota(res) -> bool:
    """429 응답 본문에서 실제로 걸린 한도가 일일(PerDay)인지 확인한다.
    파싱 실패·정보 없음은 보수적으로 False(=분당 한도로 간주, 쿨다운 후 재시도)
    처리한다 — 진짜 일일 한도 소진을 분당으로 오판해 계속 재시도하는 낭비보다,
    분당 한도를 일일로 오판해 남은 실행시간 내내 키를 놀리는 게 훨씬 큰
    손실이었기 때문이다(위 실사고 참고)."""
    try:
        body = res.json()
        for detail in body.get("error", {}).get("details", []):
            for v in detail.get("violations", []):
                qid = f"{v.get('quotaId', '')} {v.get('quotaMetric', '')}"
                if "PerDay" in qid:
                    return True
        return False
    except Exception:
        return False


class GeminiClient:
    def __init__(self, api_keys, models=None):
        self.api_keys = api_keys or []
        self.models = models or DEFAULT_GEMINI_MODELS
        # 모델별 키 상태: 없음=사용가능, True=일일 한도 소진(이 프로세스 동안
        # 영구 제외), 숫자=그 유닉스 시각 이후 재시도 가능(분당 한도 쿨다운).
        # defaultdict라 self.models에 미리 등록 안 된 모델(예: 아래 embed()의
        # 임베딩 전용 모델)도 처음 쓰이는 순간 자동으로 빈 상태에서 시작한다.
        self._exhausted_keys = defaultdict(dict)
        # 모델별 키의 마지막 호출 시각 — 가장 오래 쉰 키부터 골라 쓰는 페이싱과
        # 분당 한도 예방에 함께 쓰인다(아래 call()의 정렬 기준).
        self._last_call_at = defaultdict(dict)

    def _is_available(self, model: str, idx: int) -> bool:
        v = self._exhausted_keys[model].get(idx)
        if v is None:
            return True
        if v is True:
            return False
        return time.time() >= v

    def call(self, prompt, max_tokens=1500, start_tier=4, temperature=0.5,
             timeout=(10, 45), use_search=False, max_stages=None,
             image_bytes=None, image_mime=None):
        if not self.api_keys:
            print("[ERROR] GEMINI_API_KEY 없음")
            return None

        parts = [{"text": prompt}]
        if image_bytes:
            # 사진 워터마크 검사 등 멀티모달 호출용(2026-09-16 신설) — 이미지를
            # 텍스트 프롬프트와 함께 한 요청에 넣는다(Gemini REST 표준 형식).
            parts.append({
                "inline_data": {
                    "mime_type": image_mime or "image/jpeg",
                    "data": base64.b64encode(image_bytes).decode("ascii"),
                }
            })
        payload = {
            "contents": [{"parts": parts}],
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
            available = [i for i in range(n) if self._is_available(model, i)]
            if not available:
                print(f"  [{model}] 모든 키 소진/쿨다운 중 → 다음 모델로")
                continue

            # 마지막 호출로부터 가장 오래 쉰 키부터 시도한다 — 아래 페이싱
            # 대기 시간을 최소화하면서 자연히 5키에 고르게 분산시킨다.
            last_at = self._last_call_at[model]
            ordered = sorted(available, key=lambda i: last_at.get(i, 0))

            for idx in ordered:
                # 이 키·모델 조합의 분당 한도 예방 페이싱 — 마지막 호출 후
                # 최소 간격이 안 지났으면 나머지 시간만큼 대기 후 호출한다.
                elapsed = time.time() - last_at.get(idx, 0)
                wait = _min_interval(model) - elapsed
                if wait > 0:
                    time.sleep(wait)
                # 같은 키를 동시에 쓰는 다른 프로세스(job)가 없는지도 확인 —
                # 이 프로세스 메모리만 보는 위 계산으론 collectors/breaking/heavy가
                # 겹칠 때 합산 호출량이 한도를 넘는 걸 못 막는다.
                _wait_for_shared_slot(model, idx, _min_interval(model))

                api_key = self.api_keys[idx]
                url = (
                    f"https://generativelanguage.googleapis.com/v1beta/models/"
                    f"{model}:generateContent?key={api_key}"
                )
                try:
                    last_at[idx] = time.time()
                    res = requests.post(url, json=payload, timeout=timeout)
                    if res.status_code == 200:
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
                        _log_usage(model, idx + 1, "429")
                        if _is_per_day_quota(res):
                            print(f"  [429] {model} 키 {idx+1} 일일 한도 소진 → 이번 실행 동안 제외")
                            exhausted[idx] = True
                        else:
                            print(f"  [429] {model} 키 {idx+1} 분당 한도({_quota_info(res)}) → {KEY_COOLDOWN_SECONDS}초 후 재시도 대상, 다음 키로")
                            exhausted[idx] = time.time() + KEY_COOLDOWN_SECONDS
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

    # 2026-09-15 추가(사용자 지시 — "고민되는데... Gemini 임베딩 API 호출로는
    # 안되나?" — is_coherent_cluster()의 키워드 집합 겹침을 의미 기반으로
    # 강화하려는 시도. TF-IDF는 클러스터당 문서가 2~8건뿐이라 통계적으로
    # 무의미했음(별도 커밋 참고) — 실제 학습된 임베딩 모델을 쓰면 그 한계가
    # 없다. 생성 모델(gemini-3.x-flash 계열)과 완전히 다른 모델·엔드포인트라
    # 오늘 하루 종일 겪은 생성 모델 RPM/RPD 쿼터와는 별도 풀일 가능성이
    # 높지만, 구글이 이 한도를 대시보드 전용으로만 공개해(문서에 고정 수치
    # 없음) 확답은 못 한다 — 그래서 이 프로젝트의 기존 방침대로(_log_usage
    # 자체 집계) 실사용량을 그대로 재면서 안전하게 페이싱한다. 키 로테이션·
    # 페이싱·쿨다운은 call()과 동일한 방식을 쓰되, self.models에 없는 모델
    # 이라 별도 딕셔너리 항목("gemini-embedding-001")으로 자연히 분리된다.
    def embed(self, text: str, timeout=(10, 20)) -> list | None:
        """텍스트 임베딩 벡터(float 리스트)를 반환. 실패 시 None."""
        if not self.api_keys or not (text or "").strip():
            return None

        model = "gemini-embedding-001"
        n = len(self.api_keys)
        exhausted = self._exhausted_keys[model]
        available = [i for i in range(n) if self._is_available(model, i)]
        if not available:
            print(f"  [embed] {model} 모든 키 소진/쿨다운 중")
            return None

        last_at = self._last_call_at[model]
        ordered = sorted(available, key=lambda i: last_at.get(i, 0))

        for idx in ordered:
            elapsed = time.time() - last_at.get(idx, 0)
            wait = _EMBED_MIN_INTERVAL - elapsed
            if wait > 0:
                time.sleep(wait)
            _wait_for_shared_slot(model, idx, _EMBED_MIN_INTERVAL)

            api_key = self.api_keys[idx]
            url = (
                f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"{model}:embedContent?key={api_key}"
            )
            try:
                last_at[idx] = time.time()
                res = requests.post(
                    url,
                    json={"content": {"parts": [{"text": text[:8000]}]}},
                    timeout=timeout,
                )
                if res.status_code == 200:
                    _log_usage(model, idx + 1, "success")
                    values = (res.json().get("embedding") or {}).get("values")
                    return values or None
                elif res.status_code == 429:
                    _log_usage(model, idx + 1, "429")
                    if _is_per_day_quota(res):
                        exhausted[idx] = True
                    else:
                        exhausted[idx] = time.time() + KEY_COOLDOWN_SECONDS
                    continue
                else:
                    print(f"[ERROR] embed {res.status_code}: {res.text[:200]}")
                    _log_usage(model, idx + 1, "other")
                    return None
            except requests.exceptions.Timeout:
                print(f"  [TIMEOUT] embed 키 {idx+1} → 다음 키")
                continue
            except Exception as e:
                print(f"[ERROR] embed {e}")
                return None

        print("[ERROR] embed 모든 키 소진")
        return None
