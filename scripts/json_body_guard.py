"""
scripts/json_body_guard.py
---------------------------
Gemini가 프롬프트 지시(순수 텍스트 출력)를 무시하고 구조화 JSON 전체를
응답으로 뱉는 경우, 본문(body) 필드만 꺼내는 공용 가드.

⚠️ 2026-08-04 사고(gemini_writer.py): JSON 응답의 body 필드 "안에" JSON 전문을
   다시 써넣는 중첩 출력을 하는 경우가 있다(12건, 발행 8건). 바깥 JSON은 문법이
   정상이라 파싱을 그대로 통과해 본문이 JSON 덩어리로 저장됐다.
⚠️ 2026-08-20 사고(gemini_summarizer.py, id=89758): 이 가드가 gemini_writer.py
   에만 있고 공용화돼 있지 않아, JSON을 요청한 적도 없는 gemini_summarizer.py의
   update_summary()에서도 Gemini가 자발적으로 JSON 전체를 응답해 그대로
   summary_ko에 박힌 채 발행됐다. 저장 직전 가드는 "JSON을 요청했는가"와
   무관하게 모든 저장 경로에 있어야 한다.
"""

import json
import re

_JSON_BODY_KEY_RE = re.compile(r'"(?:body|본문)"\s*:\s*"')


def unwrap_json_body(text, _depth=0):
    """본문이 raw JSON이면 내부 body를 꺼낸다.
    반환: None=정상 본문(변경 불필요) / str=복구된 본문 / ""=JSON이지만 복구 실패

    실사고(2026-08-04~08-10, 16건 확인): 클러스터 병합 업데이트("기존 기사 업데이트")
    응답에서 Gemini가 body 필드 안에 JSON 전체를 또 넣는 경우가 있는데, 이게 2단
    이상 겹치면(바깥 body 안에 다시 body가 있는 JSON) 예전 코드는 한 겹만 벗기고
    포기해서 안쪽 JSON 그대로가 본문으로 저장됐다. depth 제한을 두고 재귀적으로
    계속 벗긴다."""
    if not text:
        return None
    s = str(text).strip()
    if not s.startswith("{"):
        return None
    head = s[:800]
    if not (_JSON_BODY_KEY_RE.search(head) or '"title"' in head or '"제목"' in head):
        return None
    j = s.rfind("}")
    for cand in (s, s[:j + 1] if j > 0 else ""):
        if not cand:
            continue
        try:
            data = json.loads(cand)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        inner = str(data.get("body") or data.get("본문") or "").strip()
        if not inner:
            continue
        if not inner.startswith("{"):
            return inner
        if _depth >= 4:
            continue  # 비정상적으로 깊게 중첩 -> 이 후보는 포기, 다음 후보 시도
        deeper = unwrap_json_body(inner, _depth + 1)
        if deeper:
            return deeper
    return ""
