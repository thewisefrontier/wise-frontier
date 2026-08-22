"""
scripts/lotto_writer.py
------------------------
로또6/45·연금복권720+·미국 파워볼 당첨 결과를 기사로 발행합니다.
"뉴스파이널과 안 어울리지만 PV 때문에" 신설(2026-08-21, 사용자 결정).

⚠️ 당첨번호·당첨금은 절대 오타가 나오면 안 된다(사용자 명시적 요구,
2026-08-21) — 그래서 이 스크립트는 Gemini를 전혀 쓰지 않는다. 본문은
API가 반환한 숫자를 Python 문자열 포맷으로 그대로 꽂아 넣는 완전
결정론적 템플릿이다. LLM이 숫자를 옮겨 적다가 하나라도 틀릴 위험 자체를
원천 차단한다.

데이터 소스:
  - 로또6/45: 동행복권 비공식 내부 API(lt645/selectPstLt645InfoNew.do,
    2026-08-21 브라우저 네트워크 로그로 확인, 인증 불필요). 예전에 흔히
    알려졌던 common.do?method=getLottoNumber는 2026년 사이트 리뉴얼로
    폐기됨(리다이렉트 응답이 Location: /error.html로 떨어지는 것까지 확인
    — 임시 차단이 아니라 진짜 죽은 라우트).
  - 연금복권720+: 동행복권 비공식 내부 API(pt720/selectPstPt720WnList.do
    + selectPstPt720WnInfo.do, 위와 동일 방식으로 확인).
  - 미국 파워볼: data.ny.gov(뉴욕주 공공데이터, 미국 정부 공식) Socrata
    API. 당첨번호·파워플레이 배수만 제공하고 잭팟·등수별 당첨자 정보는
    없음(2026-08-21 기준 별도 소스 미확인 — 추후 보강 가능).

우선순위 3종만 먼저 구현(사용자 결정, 2026-08-21) — "세계 각국 복권"
확장은 나라마다 소스를 따로 찾아야 하는 별도 프로젝트라 보류.

실행: python scripts/lotto_writer.py
권장: 1일 1회(각 복권의 실제 추첨 요일과 무관하게 매일 돌려서, 새 회차가
나온 것만 그때그때 발행 — already_published()가 중복을 막는다)
"""

import os
import requests
from datetime import date, datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

# 저장 시점 문자셋 혼입 하드 블록. LLM을 안 쓰는 스크립트라 실제로 걸릴
# 일은 거의 없지만, 다른 writer 스크립트와 동일한 안전장치를 유지한다.
try:
    from script_leak import detect_script_leak
except Exception:
    def detect_script_leak(title, body):
        return []


def now_kst() -> datetime:
    return datetime.now(timezone.utc).astimezone(KST)


def _sb_headers():
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def _sb_articles_url():
    return f"{SUPABASE_URL}/rest/v1/articles"


def format_amount(value) -> str:
    """숫자를 한국식 억/만 단위 문자열로 변환. 예: 1197258718 -> '11억 9,725만 8,718원'"""
    value = int(round(value))
    if value == 0:
        return "0원"
    eok, rem = divmod(value, 100_000_000)
    man, won = divmod(rem, 10_000)
    parts = []
    if eok:
        parts.append(f"{eok}억")
    if man:
        parts.append(f"{man:,}만")
    if won:
        parts.append(f"{won:,}")
    return " ".join(parts) + "원"


def already_published(url_key: str) -> bool:
    res = requests.get(
        f"{_sb_articles_url()}?url=eq.{url_key}&is_published=eq.true&select=id",
        headers=_sb_headers(), timeout=10,
    )
    return res.status_code in (200, 206) and len(res.json()) > 0


def insert_article(title: str, body: str, url_key: str, countries=None) -> int:
    if detect_script_leak(title, body):
        print(f"  ⚠️ [문자 혼입 감지] 저장 차단: {title[:60]}")
        return -1
    now_str = now_kst().strftime("%Y-%m-%d %H:%M")
    payload = {
        "title_en": title, "title_ko": title,
        "summary_en": "", "summary_ko": body,
        "url": url_key,
        "source": "NewsFinal",
        "category": "사회",
        "subcategory": "복권당첨정보",
        "region": "global",
        "country": (countries or ["한국"])[0],
        "country_flag": "",
        "countries": countries or ["한국"],
        "image_url": "",
        "score": 1,
        "created_at": now_str,
        "first_published_at": now_str,
        "update_log": [{"timestamp": now_str, "note": "복권 당첨정보 자동 기사"}],
        "sent_telegram": 0,
        "is_published": True,
    }
    headers = {**_sb_headers(), "Prefer": "resolution=ignore-duplicates,return=representation"}
    res = requests.post(_sb_articles_url(), headers=headers, json=payload, timeout=15)
    if res.status_code in (200, 201):
        data = res.json()
        return data[0].get("id", -1) if data else -1
    print(f"  [ERROR] 기사 삽입 실패 {res.status_code}: {res.text[:200]}")
    return -1


# ── 로또 6/45 ──────────────────────────────────────────────
def get_expected_lotto_round(today: date = None) -> int:
    """오늘 기준 가장 최근 추첨 회차(토요일 기준) 계산. 1회 추첨일 2002-12-07 기준."""
    today = today or now_kst().date()
    d0 = date(2002, 12, 7)
    days_since_sat = (today.weekday() - 5) % 7  # 토요일=5
    last_saturday = today - timedelta(days=days_since_sat)
    return (last_saturday - d0).days // 7 + 1


def fetch_lotto645(round_no: int) -> dict | None:
    try:
        res = requests.get(
            "https://www.dhlottery.co.kr/lt645/selectPstLt645InfoNew.do",
            params={"srchDir": "center", "srchLtEpsd": round_no},
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.dhlottery.co.kr/lt645/result"},
            timeout=15,
        )
        if res.status_code != 200:
            print(f"  [WARN] 로또 조회 실패: HTTP {res.status_code}")
            return None
        items = res.json().get("data", {}).get("list", [])
        for item in items:
            if item.get("ltEpsd") == round_no:
                return item
        return None
    except Exception as e:
        print(f"  [WARN] 로또 조회 실패: {e}")
        return None


def build_lotto645_article(d: dict) -> tuple[str, str, str]:
    nums = [d[f"tm{i}WnNo"] for i in range(1, 7)]
    bonus = d["bnsWnNo"]
    round_no = d["ltEpsd"]
    draw_date = datetime.strptime(d["ltRflYmd"], "%Y%m%d").date()

    nums_str = ", ".join(str(n) for n in nums)
    title = f"[복권] 로또 {round_no}회 당첨번호 {nums_str}…보너스 {bonus}"

    body = (
        f"동행복권이 {draw_date.day}일 추첨한 로또6/45 {round_no}회 당첨번호를 발표했다. "
        f"당첨번호는 {nums_str}이며 보너스 번호는 {bonus}다.\n\n"
        f"1등 당첨자는 {d['rnk1WnNope']:,}명으로 각자 {format_amount(d['rnk1WnAmt'])}씩 받는다. "
        f"1등 총 당첨금은 {format_amount(d['rnk1SumWnAmt'])}이다.\n"
        f"2등 당첨자는 {d['rnk2WnNope']:,}명으로 각자 {format_amount(d['rnk2WnAmt'])}씩 받는다.\n"
        f"3등 당첨자는 {d['rnk3WnNope']:,}명으로 각자 {format_amount(d['rnk3WnAmt'])}씩 받는다.\n"
        f"4등(5개 번호 일치)은 {d['rnk4WnNope']:,}명으로 각자 {format_amount(d['rnk4WnAmt'])}씩, "
        f"5등(4개 번호 일치)은 {d['rnk5WnNope']:,}명으로 각자 {format_amount(d['rnk5WnAmt'])}씩 받는다.\n\n"
        f"이번 {round_no}회차 총 판매금액은 {format_amount(d['rlvtEpsdSumNtslAmt'])}이었다."
    )
    url_key = f"internal://lotto645_{round_no}"
    return title, body, url_key


# ── 연금복권720+ ────────────────────────────────────────────
def fetch_pension720_latest() -> dict | None:
    try:
        res = requests.get(
            "https://www.dhlottery.co.kr/pt720/selectPstPt720WnList.do",
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.dhlottery.co.kr/pt720/result"},
            timeout=15,
        )
        if res.status_code != 200:
            print(f"  [WARN] 연금복권 조회 실패: HTTP {res.status_code}")
            return None
        items = res.json().get("data", {}).get("result", [])
        return items[0] if items else None
    except Exception as e:
        print(f"  [WARN] 연금복권 조회 실패: {e}")
        return None


def fetch_pension720_prizes(round_no: int) -> list:
    try:
        res = requests.get(
            "https://www.dhlottery.co.kr/pt720/selectPstPt720WnInfo.do",
            params={"srchPsltEpsd": round_no},
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.dhlottery.co.kr/pt720/result"},
            timeout=15,
        )
        if res.status_code != 200:
            return []
        return res.json().get("data", {}).get("result", [])
    except Exception as e:
        print(f"  [WARN] 연금복권 당첨금 조회 실패: {e}")
        return []


# 연금복권720+ 등수별 당첨금은 매 회차 고정이다(사용자 확인, 2026-08-21).
# API의 wnAmt/totAmt 필드 의미가 불명확해서(예: 3등 wnAmt=3,700만원인데
# 실제 고정 당첨금은 100만원 — 다른 걸 가리키는 필드로 추정) 이 필드들을
# 아예 쓰지 않고, 공식 발표 고정액을 그대로 상수로 박아둔다. API에서는
# 회차마다 변하는 "당첨자 수"(wnTotalCnt)만 가져온다 — 금액을 API 필드
# 해석에 의존하지 않아야 오탐 위험이 없다(사용자 요구: "절대 오타 금지").
_PT720_RANK_INFO = {
    # rank: (라벨, 고정 당첨금 문구)
    1: ("1등", "매월 700만원씩 20년(세전 총 16억 8,000만원)"),
    2: ("2등", "매월 100만원씩 10년(세전 총 1억 2,000만원)"),
    3: ("3등", "100만원"),
    4: ("4등", "10만원"),
    5: ("5등", "5만원"),
    6: ("6등", "5,000원"),
    7: ("7등", "1,000원"),
    8: ("보너스", "매월 100만원씩 10년(세전 총 1억 2,000만원)"),
}


def build_pension720_article(latest: dict, prizes: list) -> tuple[str, str, str]:
    round_no = latest["psltEpsd"]
    draw_date = datetime.strptime(latest["psltRflYmd"], "%Y%m%d").date()
    bnd = latest["wnBndNo"]
    num = latest["wnRnkVl"]
    bonus_num = latest["bnsRnkVl"]

    title = f"[복권] 연금복권720+ {round_no}회 당첨번호 {bnd}조 {num}"

    prize_lines = []
    by_rank = {p["wnRnk"]: p for p in prizes}
    for rank in (1, 2, 3, 4, 5):
        p = by_rank.get(rank)
        if not p or not p.get("wnTotalCnt"):
            continue
        label, amount_desc = _PT720_RANK_INFO.get(rank, (f"{rank}등", ""))
        prize_lines.append(
            f"{label} 당첨자는 {p['wnTotalCnt']:,}명으로, 당첨금은 {amount_desc}이다."
        )

    body = (
        f"동행복권이 {draw_date.day}일 추첨한 연금복권720+ {round_no}회 당첨번호를 발표했다. "
        f"당첨번호는 {bnd}조 {num}이며 보너스 번호는 {bonus_num}다.\n\n"
        + "\n".join(prize_lines)
    )
    url_key = f"internal://pension720_{round_no}"
    return title, body, url_key


# ── 미국 파워볼 ──────────────────────────────────────────────
def fetch_powerball_latest() -> dict | None:
    try:
        res = requests.get(
            "https://data.ny.gov/resource/d6yy-54nr.json",
            params={"$limit": 1, "$order": "draw_date DESC"},
            timeout=15,
        )
        if res.status_code != 200:
            print(f"  [WARN] 파워볼 조회 실패: HTTP {res.status_code}")
            return None
        items = res.json()
        return items[0] if items else None
    except Exception as e:
        print(f"  [WARN] 파워볼 조회 실패: {e}")
        return None


def build_powerball_article(d: dict) -> tuple[str, str, str]:
    draw_date = datetime.strptime(d["draw_date"][:10], "%Y-%m-%d").date()
    nums = [int(n) for n in d["winning_numbers"].split()]
    white_balls, powerball = nums[:5], nums[5]
    multiplier = d.get("multiplier", "")

    nums_str = ", ".join(str(n) for n in white_balls)
    title = f"[복권] 美 파워볼 {draw_date.month}월 {draw_date.day}일 당첨번호 {nums_str}+파워볼 {powerball}"

    body = (
        f"미국 파워볼이 {draw_date.month}월 {draw_date.day}일(현지시간) 추첨한 당첨번호를 발표했다. "
        f"당첨번호는 {nums_str}이며 파워볼 번호는 {powerball}이다."
        + (f" 이날 파워플레이 배수는 {multiplier}배였다." if multiplier else "")
    )
    url_key = f"internal://powerball_{d['draw_date'][:10]}"
    return title, body, url_key


# ── 메인 ─────────────────────────────────────────────────────
def run():
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("[SKIP] SUPABASE 환경변수 없음")
        return

    print(f"\n[lotto_writer] 시작: {now_kst().strftime('%Y-%m-%d %H:%M')} KST")

    # 로또 6/45
    round_no = get_expected_lotto_round()
    lotto_url = f"internal://lotto645_{round_no}"
    if already_published(lotto_url):
        print(f"  → 로또 {round_no}회 이미 발행됨 → 스킵")
    else:
        d = fetch_lotto645(round_no)
        if d:
            title, body, url_key = build_lotto645_article(d)
            aid = insert_article(title, body, url_key, countries=["한국"])
            print(f"  {'✓' if aid > 0 else '✗'} 로또 {round_no}회: id={aid}")
        else:
            print(f"  → 로또 {round_no}회 데이터 없음(아직 추첨 전이거나 회차 계산 오류)")

    # 연금복권720+
    latest = fetch_pension720_latest()
    if latest:
        p_round = latest["psltEpsd"]
        p_url = f"internal://pension720_{p_round}"
        if already_published(p_url):
            print(f"  → 연금복권 {p_round}회 이미 발행됨 → 스킵")
        else:
            prizes = fetch_pension720_prizes(p_round)
            title, body, url_key = build_pension720_article(latest, prizes)
            aid = insert_article(title, body, url_key, countries=["한국"])
            print(f"  {'✓' if aid > 0 else '✗'} 연금복권 {p_round}회: id={aid}")
    else:
        print("  → 연금복권 데이터 조회 실패")

    # 미국 파워볼
    pb = fetch_powerball_latest()
    if pb:
        pb_date = pb["draw_date"][:10]
        pb_url = f"internal://powerball_{pb_date}"
        if already_published(pb_url):
            print(f"  → 파워볼 {pb_date} 이미 발행됨 → 스킵")
        else:
            title, body, url_key = build_powerball_article(pb)
            aid = insert_article(title, body, url_key, countries=["미국"])
            print(f"  {'✓' if aid > 0 else '✗'} 파워볼 {pb_date}: id={aid}")
    else:
        print("  → 파워볼 데이터 조회 실패")

    print(f"[lotto_writer] 완료: {now_kst().strftime('%Y-%m-%d %H:%M')} KST")


if __name__ == "__main__":
    run()
