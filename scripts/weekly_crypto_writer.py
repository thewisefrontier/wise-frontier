"""
scripts/weekly_crypto_writer.py
--------------------------------
일요일 저녁, 이번 주 비트코인·이더리움 시세와 그 주 실제 보도를 근거로 한
"주간 코인시황" 서사형 기사를 자동 생성합니다.

2026-09-27 신설(사용자 요청 — weekly_market_card.py의 "주간 시세 카드"는
토요일에 나가는 숫자 전용 카드인데, 코인은 24시간 거래라 일요일 자정까지
봐야 한 주가 진짜로 끝난다 + "코인 종합시황"은 가격만이 아니라 그 주
이슈까지 담아야 한다는 요청). 가격 계산은 weekly_market_card.py의
weekly_change()(2026-09-26에 이미 "월~금이 아니라 최신 종가 vs 정확히
7일 전"으로 고쳐둔 크립토 전용 분기)를 그대로 재사용 — 중복 구현 안 함.

설계 원칙(oil_price_writer.py와 동일한, 이 프로젝트에서 검증된 패턴):
  - 가격 수치는 100% 코드 계산(LLM이 못 지어냄).
  - 그 주 "이슈" 서술은 구글 뉴스 실제 헤드라인만 근거로 쓰게 하고,
    헤드라인에 없는 사실은 지어내지 말라고 명시 지시.
  - 저장 전 날조 검사(fabrication_guard)로 한 번 더 걸러냄.

실행: python scripts/weekly_crypto_writer.py
      python scripts/weekly_crypto_writer.py --dry-run
"""

import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone

from dotenv import load_dotenv

load_dotenv()

try:
    from news_context import fetch_headlines
except Exception:
    def fetch_headlines(*a, **k):
        return []

try:
    from script_leak import detect_script_leak
except Exception:
    def detect_script_leak(title, body):
        return []

try:
    from json_body_guard import unwrap_json_body
except Exception:
    def unwrap_json_body(text, _depth=0):
        return None

try:
    from article_store import insert_final_article, sb_headers as _sb_headers, sb_url as _sb_url
except Exception:
    import requests
    SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
    SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
    def _sb_headers():
        return {"apikey": SUPABASE_SERVICE_KEY, "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                "Content-Type": "application/json", "Prefer": "return=minimal"}
    def _sb_url(table="articles"):
        return f"{SUPABASE_URL}/rest/v1/{table}"
    def insert_final_article(payload: dict) -> int:
        headers = {**_sb_headers(), "Prefer": "resolution=ignore-duplicates,return=representation"}
        res = requests.post(_sb_url(), headers=headers, json=payload, timeout=15)
        if res.status_code in (200, 201):
            data = res.json()
            return data[0].get("id", -1) if data else -1
        return -1

try:
    from style_guard import (parse_article_output, ensure_paragraphs,
                              enforce_title_prefix as _sg_enforce_title_prefix,
                              has_column_style, has_polite_ending, to_plain_style)
except Exception:
    def ensure_paragraphs(text, target=3, max_sentences_per_para=4):
        return text
    def _sg_enforce_title_prefix(title, prefix, bare_name, particles=None):
        return f"{prefix} {(title or '').strip()}".strip()
    def parse_article_output(text):
        m_title = re.search(r"TITLE:\s*(.+?)(?:\n|$)", text)
        m_body = re.search(r"BODY:\s*([\s\S]+)", text)
        return ((m_title.group(1).strip() if m_title else ""),
                (m_body.group(1).strip() if m_body else ""))
    def has_column_style(text):
        return any(p in text for p in ("주목됩니다", "기대됩니다", "보여줍니다", "시사합니다"))
    def has_polite_ending(text):
        return False
    def to_plain_style(text):
        return text

try:
    from fabrication_guard import verify_no_fabricated_names as _fg_verify_no_fabricated_names
except Exception:
    def _fg_verify_no_fabricated_names(source_prompt, body, call_gemini_fn):
        return ""

GEMINI_MODELS = [
    "gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash",
    "gemini-3.5-flash-lite", "gemini-3.1-flash-lite",
]
GEMINI_API_KEYS = [k for k in [
    os.getenv("GEMINI_API_KEY"), os.getenv("GEMINI_API_KEY_2"), os.getenv("GEMINI_API_KEY_3"),
    os.getenv("GEMINI_API_KEY_4"), os.getenv("GEMINI_API_KEY_5"),
] if k]

try:
    from gemini_client import GeminiClient
except Exception:
    class GeminiClient:
        def __init__(self, *a, **k):
            pass
        def call(self, *a, **k):
            return None

_gemini_client = GeminiClient(GEMINI_API_KEYS, GEMINI_MODELS)

KST = timezone(timedelta(hours=9))
TITLE_PREFIX = "[주간 코인시황]"
# 2026-09-27 실측: 600자로 뒀더니 5회 시도 중 4회가 스킵됐다(내용 품질 자체는
# 문제없었음 — 재작성 유발 조건이 예민해 재생성본이 종종 짧게 나옴). 이 프로젝트의
# 다른 데이터기사(oil_price_writer.py)와 같은 500자 기준으로 맞춤.
MIN_BODY_LEN = 500
# 일요일 저녁 이후로 미룬다 — 그 전엔 그 주 마지막 하루치(토·일) 변동이
# 덜 반영된 상태다. 정확한 "자정 확정"이 필요한 주식과 달리 크립토는
# 매 실행 시점의 최신 종가를 그대로 쓰므로(weekly_change 참고), 이 시각은
# "충분히 늦은 시점"이면 되고 엄밀한 마감 확인이 아니다.
PUBLISH_HOUR_KST = 20


def now_kst() -> datetime:
    return datetime.now(timezone.utc).astimezone(KST)


def call_gemini(prompt: str, max_tokens: int = 2000, start_tier: int = 4) -> str | None:
    return _gemini_client.call(prompt, max_tokens=max_tokens, start_tier=start_tier,
                                temperature=0.2, timeout=(10, 60))


def verify_no_fabricated_names(source_prompt: str, body: str) -> str:
    return _fg_verify_no_fabricated_names(source_prompt, body, call_gemini)


def enforce_title_prefix(title: str) -> str:
    # bare_name은 대괄호 "안"에 들어가는 문구와 정확히 같아야 중복 부착이 걸러진다
    # (style_guard.enforce_title_prefix가 "[bare_name]"을 통째로 매칭) — TITLE_PREFIX와
    # 짝을 맞출 것. 2026-09-27 실사고: bare_name을 "코인"으로 줬다가 Gemini가 프롬프트
    # 지시대로 이미 "[주간 코인시황]"을 붙여 왔는데 매칭이 안 돼 접두어가 중복됐다.
    return _sg_enforce_title_prefix(title, TITLE_PREFIX, "주간 코인시황", particles=("이", "은"))


def already_published(week_end: date) -> bool:
    res = __import__("requests").get(
        _sb_url(), headers=_sb_headers(),
        params={"select": "id", "url": f"eq.internal://weekly_crypto_{week_end.isoformat()}", "limit": "1"},
        timeout=10,
    )
    return res.status_code in (200, 206) and bool(res.json())


def build_article_prompt(btc: dict, eth: dict) -> str:
    def line(name, d):
        arrow = "상승" if d["pct"] > 0 else ("하락" if d["pct"] < 0 else "보합")
        return (f"- {name}: {d['end_date'].month}월 {d['end_date'].day}일 기준 "
                f"{d['end']:,.0f}달러 (7일 전 {d['prev_date'].month}월 {d['prev_date'].day}일 "
                f"{d['prev']:,.0f}달러 대비 {d['pct']:+.2f}% {arrow})")

    headlines = fetch_headlines("bitcoin cryptocurrency market this week", limit=8)
    if headlines:
        headline_block = "\n[관련 실제 보도 헤드라인 (참고용, 최신순이라 정확한 날짜는 불명확할 수 있음)]\n" + \
            "\n".join(f"- {h}" for h in headlines)
        bg_instruction = (
            "② 이번 주 이슈: 위 [관련 실제 보도 헤드라인]에 실제로 나온 사건·이슈만 근거로 서술하세요. "
            "⚠️ 헤드라인 속 가격 수치·퍼센트는 절대 인용하지 마세요(시점이 다를 수 있음) — 가격은 "
            "오직 위 [가격 데이터]만 쓰세요. ⚠️ 헤드라인에 없는 규제·정책·인물 발언·기업 동향을 "
            "지어내지 마세요. 헤드라인 중 이번 주와 무관해 보이는 게 섞여 있으면 그냥 무시하세요. "
            "실제로 관련된 헤드라인이 하나도 없으면 억지로 채우지 말고 가격 흐름 설명에 집중하세요. "
            "특정 매체를 직접 인용하지 말고 \"~인 것으로 전해졌다\", \"~라는 소식이 나왔다\"처럼 "
            "자연스럽게 녹여 쓰세요."
        )
    else:
        headline_block = ""
        bg_instruction = "② 이번 주 이슈: 가격 흐름과 시장 전반의 매수·매도 심리 위주로 간단히"

    return f"""당신은 가상자산 시장 전문 기자입니다.
아래 데이터를 바탕으로 이번 한 주(지난 일요일~오늘) 코인 시황 기사를 작성하세요.

[가격 데이터] (출처: 야후 파이낸스)
{line("비트코인(BTC)", btc)}
{line("이더리움(ETH)", eth)}
{headline_block}

[출력 형식] — 반드시 이 형식 그대로:
TITLE: <제목>
BODY: <본문>

[제목]
- 반드시 "{TITLE_PREFIX} "로 시작. 대괄호 포함 그대로 출력.
- 이번 주 핵심 동인 하나를 담아 대괄호 포함 55자 이내.
- 예: "{TITLE_PREFIX} 비트코인 한 주간 3%대 상승…8만달러 안착"

[본문]
① 이번 주 가격 흐름: 비트코인·이더리움 각각의 등락을 위 [가격 데이터] 수치 그대로 서술.
{bg_instruction}
③ 마무리: 다음 주 관전 포인트를 1~2문장으로(단정적 전망·투자 권유 금지 — "~로 주목된다" 수준까지만).

- 총 4~6개 문단. 각 문단 2~4문장.
- 합쇼체(-습니다) 금지, "-다"체로 서술.
- 이 기사가 개인의 투자 조언이 아니라는 점을 마지막에 짧게 덧붙이세요(예: "이 기사는 투자 참고용
  정보 제공을 목적으로 하며, 투자 판단과 책임은 본인에게 있다.").
"""


_IMAGE_KEYWORDS = ["bitcoin coin", "bitcoin cryptocurrency logo"]


def fetch_crypto_image(week_end: date) -> str:
    from article_image import fetch_seeded_pixabay_image
    return fetch_seeded_pixabay_image(_IMAGE_KEYWORDS, week_end.toordinal(), f"weekly_crypto_{week_end.isoformat()}")


def insert_article(title_ko: str, summary_ko: str, week_end: date, btc: dict, eth: dict, image_url: str = "") -> int:
    if detect_script_leak(title_ko, summary_ko):
        print(f"  ⚠️ [문자 혼입 감지] 저장 차단: {title_ko[:60]}")
        return -1
    _unwrapped = unwrap_json_body(summary_ko)
    if _unwrapped is not None:
        if _unwrapped:
            print("  🔧 [raw JSON 본문] 내부 body 추출 → 복구")
            summary_ko = _unwrapped
        else:
            print(f"  ⛔ [raw JSON 본문] 저장 차단: {title_ko[:60]}")
            return -1

    now_str = now_kst().strftime("%Y-%m-%d %H:%M")
    payload = {
        "title_en": title_ko, "title_ko": title_ko, "summary_en": "", "summary_ko": summary_ko,
        "url": f"internal://weekly_crypto_{week_end.isoformat()}",
        "source": "NewsFinal", "category": "경제", "subcategory": "주간코인시황",
        "region": "글로벌", "country": "", "country_flag": "", "image_url": image_url, "countries": [],
        "score": 1, "created_at": now_str, "first_published_at": now_str,
        "update_log": [{"timestamp": now_str, "note": "주간 코인시황 자동 기사"}],
        "source_data": {
            "week_end": week_end.isoformat(),
            "btc": {**btc, "end_date": btc["end_date"].isoformat(), "prev_date": btc["prev_date"].isoformat()},
            "eth": {**eth, "end_date": eth["end_date"].isoformat(), "prev_date": eth["prev_date"].isoformat()},
        },
        "sent_telegram": 0, "is_published": True,
    }
    return insert_final_article(payload)


def main():
    dry = "--dry-run" in sys.argv
    now = now_kst()
    print(f"\n[weekly_crypto_writer] 시작: {now.strftime('%Y-%m-%d %H:%M')} KST")

    if not dry and (now.weekday() != 6 or now.hour < PUBLISH_HOUR_KST):
        print(f"  → 일요일 {PUBLISH_HOUR_KST}시(KST) 이전 → 스킵")
        return

    week_end = now.date()
    if not dry and already_published(week_end):
        print(f"  → {week_end} 주간 코인시황 이미 발행됨 → 스킵")
        return

    from weekly_market_card import weekly_change
    btc = weekly_change("BTC-USD", week_end, limit_pct=1000)
    eth = weekly_change("ETH-USD", week_end, limit_pct=1000)
    if not btc or not eth:
        print("  [ERROR] 가격 데이터 수집 실패(BTC/ETH 중 하나 이상 실패) → 종료")
        return

    prompt = build_article_prompt(btc, eth)
    if dry:
        print(prompt)

    article_text = call_gemini(prompt, max_tokens=2000, start_tier=4)
    time.sleep(8)
    if not article_text:
        print("  [ERROR] 기사 생성 실패")
        return

    if has_column_style(article_text):
        print("  ⚠️ 논평체 감지 → 재생성")
        article_text = call_gemini(
            prompt + "\n\n[재작성 지시] 논평/칼럼 문체나 투자 권유성 표현이 섞였습니다. 사실 전달 중심으로만 다시 작성하세요.",
            max_tokens=2000, start_tier=4,
        ) or article_text
        time.sleep(5)

    fabricated = verify_no_fabricated_names(prompt, article_text)
    if fabricated:
        print(f"  ⚠️ 원문에 없는 고유명사 감지({fabricated}) → 재생성")
        article_text = call_gemini(
            prompt + f"\n\n[재작성 지시] 다음을 원문에 없는 내용으로 잘못 지어냈습니다: {fabricated}. "
                     "헤드라인에 실제로 나온 내용만 쓰고, 확신할 수 없으면 언급하지 마세요.",
            max_tokens=2000, start_tier=4,
        ) or article_text
        time.sleep(5)

    if has_polite_ending(article_text):
        converted = to_plain_style(article_text)
        if converted != article_text:
            print("  🔧 합쇼체(-습니다) 감지 → 자동 변환 적용")
            article_text = converted

    art_title, art_body = parse_article_output(article_text)
    art_title = enforce_title_prefix(art_title)
    if not art_title or not art_body:
        print(f"  [ERROR] TITLE/BODY 파싱 실패\n{article_text[:300]}")
        return

    art_body = ensure_paragraphs(art_body)
    if len(art_body) < MIN_BODY_LEN:
        print(f"  ⚠️ 본문 너무 짧음 ({len(art_body)}자) → 스킵")
        return

    print(f"  → 제목: {art_title}")
    print(f"  → 본문 {len(art_body)}자")

    if dry:
        print("\n" + art_title + "\n\n" + art_body)
        return

    image_url = fetch_crypto_image(week_end)
    art_id = insert_article(art_title, art_body, week_end, btc, eth, image_url)
    if art_id > 0:
        print(f"  ✓ 기사 삽입 완료 (articles.id={art_id})")
    else:
        print("  [ERROR] 기사 삽입 실패")


if __name__ == "__main__":
    main()
