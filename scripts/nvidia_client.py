"""
scripts/nvidia_client.py
--------------------------
NVIDIA NIM(build.nvidia.com) API의 얇은 클라이언트. "같은 모델(Gemini)이 기사를
쓰고 같은 모델이 스스로 검증하면 맹점이 그대로 반복된다"는 문제(2026-08-24
사용자 지적)를 줄이기 위해, 계열이 다른 모델로 2차 판단을 받는 용도로 쓴다.

⚠️ 2026-09-03 정정: 이전엔 "무료 티어가 월 1,000 크레딧 한도"라고 알고
있었는데(서드파티 블로그발 오정보), 사용자가 본인 build.nvidia.com 계정을
직접 확인해보니 실제 표시되는 제한은 40RPM뿐이고 크레딧 한도는 없었다
(memory: newsfinal_nvidia_cross_verification 참조). 그래서 verify_entities.py
도 "1차 판단이 의심 확정한 소수 건"뿐 아니라 배치 전체를 독립 재검토하도록
확대했고, gemini_summarizer.py의 트렌드 중복판정(_same_event_llm)도 여기로
옮겼다 — 호출 빈도가 낮은(전체 배치 대상은 아닌) 곳부터 우선 적용, RPM만
호출 간 sleep으로 지키면 된다.

모델: nvidia/nemotron-3-ultra-550b-a55b (561B MoE, 월 5천만+ 콜로 안정적으로
운영 중인 걸 확인, thinking 모델이라 enable_thinking=False 필수 — 켜두면 짧은
max_tokens에서 응답 전 추론 단계만 하다 잘린다, 2026-08-24 실측).
"""

import os
import time
import requests

NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY_L") or os.getenv("NVIDIA_API_KEY", "")
NVIDIA_MODEL = "nvidia/nemotron-3-ultra-550b-a55b"
NVIDIA_URL = "https://integrate.api.nvidia.com/v1/chat/completions"

# ── 한도 관리 (2026-09-22 신설) ──────────────────────────────
# 사용자 지시: "제미나이나 엔비디아 같은 모델 한도가 입력이 돼 있잖아. 그 부분을
# 감안해서 구조를 짜고 변경하라고."
#
# 이 계정의 실제 제한은 40RPM(크레딧 한도 없음 — 위 docstring 참조)인데, 종전엔
# 이 클라이언트에 페이싱도 429 처리도 없었다. 호출부가 6개 파일 7곳으로 늘어난
# 상태에서 각자 알아서 sleep하는 구조라, 합산이 한도를 넘는지 아무도 안 보고 있었다.
# 특히 verify_entities.py는 별도 워크플로(verify_entities.yml, 매일 KST 06:30)라
# 매시간 도는 run.yml과 겹칠 수 있고, 그것 하나가 sleep(2초)=분당 30건으로 예산의
# 75%를 쓴다. 겹치는 시간대에 429가 나면 종전 코드는 그냥 None을 반환했고,
# 호출부 대부분이 "판정 실패 = 통과"로 처리하므로 검증이 조용히 꺼졌다.
#
# 그래서 두 층으로 막는다.
#   1) 프로세스 내 최소 간격 — 자기 자신이 버스트로 한도를 태우는 걸 막는다.
#   2) 429/5xx 재시도 — 다른 워크플로와 겹쳐 한도를 넘었을 때 포기하지 않고 기다린다.
#      (5xx는 실측 필요: 밀도 판정 실험에서 18콜 중 2콜이 503이었다)
NVIDIA_MIN_INTERVAL = float(os.getenv("NVIDIA_MIN_INTERVAL", "1.5"))  # 1.5초 = 분당 40건(계정 한도와 동일)
_RATE_LIMIT_WAIT = 15   # 429는 RPM 윈도우(60초)가 도는 중이라는 뜻 — 잠깐 기다리면 풀린다
_SERVER_ERR_WAIT = 3
_MAX_ATTEMPTS = 3

_last_call_at = 0.0


def _pace():
    """직전 호출로부터 최소 간격이 지나도록 대기."""
    global _last_call_at
    wait = NVIDIA_MIN_INTERVAL - (time.monotonic() - _last_call_at)
    if wait > 0:
        time.sleep(wait)
    _last_call_at = time.monotonic()


def call_nvidia(prompt: str, max_tokens: int = 400, temperature: float = 0.2) -> str | None:
    """실패 시 None을 반환한다(호출부는 대부분 fail-open으로 처리)."""
    if not NVIDIA_API_KEY:
        return None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        _pace()
        try:
            res = requests.post(
                NVIDIA_URL,
                headers={"Authorization": f"Bearer {NVIDIA_API_KEY}", "Content-Type": "application/json"},
                json={
                    "model": NVIDIA_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "chat_template_kwargs": {"enable_thinking": False},
                },
                timeout=60,
            )
        except Exception as e:
            print(f"  [NVIDIA ERROR] {e}")
            if attempt < _MAX_ATTEMPTS:
                time.sleep(_SERVER_ERR_WAIT)
                continue
            return None

        if res.status_code == 200:
            choices = res.json().get("choices", [])
            if not choices:
                return None
            text = (choices[0].get("message", {}).get("content") or "").strip()
            return text or None

        # 429(RPM 초과)와 5xx(일시적 과부하)는 기다렸다 다시 시도한다.
        retryable = res.status_code == 429 or 500 <= res.status_code < 600
        print(f"  [NVIDIA] {res.status_code} (시도 {attempt}/{_MAX_ATTEMPTS}): {res.text[:150]}")
        if not retryable or attempt == _MAX_ATTEMPTS:
            return None
        time.sleep(_RATE_LIMIT_WAIT if res.status_code == 429 else _SERVER_ERR_WAIT)
    return None
