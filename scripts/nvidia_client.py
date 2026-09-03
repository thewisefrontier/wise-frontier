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
import requests

NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY_L") or os.getenv("NVIDIA_API_KEY", "")
NVIDIA_MODEL = "nvidia/nemotron-3-ultra-550b-a55b"
NVIDIA_URL = "https://integrate.api.nvidia.com/v1/chat/completions"


def call_nvidia(prompt: str, max_tokens: int = 400, temperature: float = 0.2) -> str | None:
    if not NVIDIA_API_KEY:
        return None
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
        if res.status_code != 200:
            print(f"  [NVIDIA] {res.status_code}: {res.text[:200]}")
            return None
        choices = res.json().get("choices", [])
        if not choices:
            return None
        text = (choices[0].get("message", {}).get("content") or "").strip()
        return text or None
    except Exception as e:
        print(f"  [NVIDIA ERROR] {e}")
        return None
