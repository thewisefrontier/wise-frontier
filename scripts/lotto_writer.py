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
import time
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

# articles 테이블 삽입 공용 로직(2026-09-02, article_store.py로 공용화).
try:
    from article_store import insert_final_article
except Exception:
    def insert_final_article(payload: dict) -> int:
        headers = {**_sb_headers(), "Prefer": "resolution=ignore-duplicates,return=representation"}
        res = requests.post(_sb_articles_url(), headers=headers, json=payload, timeout=15)
        if res.status_code in (200, 201):
            data = res.json()
            return data[0].get("id", -1) if data else -1
        return -1


def now_kst() -> datetime:
    return datetime.now(timezone.utc).astimezone(KST)


# Supabase 헤더/URL 헬퍼는 article_store.py로 공용화(2026-09-02).
try:
    from article_store import sb_headers as _sb_headers
    from article_store import sb_url as _sb_articles_url
except Exception:
    def _sb_headers():
        return {
            "apikey": SUPABASE_SERVICE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }
    def _sb_articles_url():
        return f"{SUPABASE_URL}/rest/v1/articles"


def _to_man_units(value) -> str:
    """숫자를 한국식 억/만 단위로 그룹핑. 억/만 단위 자체가 자릿수 구분 역할을
    하므로 그룹 내부에는 콤마(,)를 쓰지 않는다(예: '1,478만'이 아니라 '1478만').
    예: 1197258718 -> '11억 9725만 8718'"""
    value = int(round(value))
    if value == 0:
        return "0"
    eok, rem = divmod(value, 100_000_000)
    man, won = divmod(rem, 10_000)
    parts = []
    if eok:
        parts.append(f"{eok}억")
    if man:
        parts.append(f"{man}만")
    if won:
        parts.append(f"{won}")
    return " ".join(parts)


def format_amount(value) -> str:
    """숫자를 한국식 억/만 단위 금액 문자열로 변환. 예: 1197258718 -> '11억 9725만 8718원'"""
    if int(round(value)) == 0:
        return "0원"
    return _to_man_units(value) + "원"


def format_count(value) -> str:
    """인원수 등을 한국식 단위로 변환. 1만 미만은 콤마 구분(예: '3,081'),
    1만 이상은 억/만 단위로 그룹핑(예: 152825 -> '15만 2825')."""
    value = int(value)
    if value < 10_000:
        return f"{value:,}"
    return _to_man_units(value)


def already_published(url_key: str) -> bool:
    res = requests.get(
        f"{_sb_articles_url()}?url=eq.{url_key}&is_published=eq.true&select=id",
        headers=_sb_headers(), timeout=10,
    )
    return res.status_code in (200, 206) and len(res.json()) > 0


def insert_article(title: str, body: str, url_key: str, countries=None, image_url: str = "") -> int:
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
        "image_url": image_url,
        # 2026-09-03 수정: 이 이미지는 동행복권 결과 페이지 캡처/자체 렌더링이지
        # Pixabay가 아니다. image_credit을 비워두면 프론트가 R2 호스팅 URL만
        # 보고 "이미지 출처: Pixabay"로 잘못 표시한다(같은 R2 버킷을 Pixabay
        # 캐시에도 쓰기 때문 — image-credit.js 참조). 명시적으로 채워 오표기를 막는다.
        "image_credit": "뉴스파이널",
        "score": 1,
        "created_at": now_str,
        "first_published_at": now_str,
        "update_log": [{"timestamp": now_str, "note": "복권 당첨정보 자동 기사"}],
        "sent_telegram": 0,
        "is_published": True,
    }
    return insert_final_article(payload)


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


# 동행복권 공식 공 색상 규칙(1-10 노랑/11-20 파랑/21-30 빨강/31-40 회색/41-45 초록).
_BALL_COLOR_BANDS = [(10, "#FBC400"), (20, "#69C8F2"), (30, "#FF7272"), (40, "#AAAAAA"), (45, "#B0D840")]


def _ball_color(n: int) -> str:
    for upper, color in _BALL_COLOR_BANDS:
        if n <= upper:
            return color
    return "#B0D840"


# 자체 생성 이미지용 한글 폰트(Noto Sans KR Bold를 이 스크립트가 실제로 쓰는
# 문구만 남기고 서브셋 — 원본은 ~4.8MB, 서브셋은 ~10KB). CI(우분투 러너)에
# 한글 폰트가 기본 설치되어 있지 않아 자체 폰트를 커밋해 둔다(2026-08-31,
# 사용자 지시: "한글로 바꿀까요?" → "응"). 서브셋 재생성:
#   py -m fontTools.subset <원본 폰트> --text="<필요 문자 전부>" \
#     --output-file=scripts/assets/NotoSansKR-Bold-subset.otf
_KR_FONT_PATH = os.path.join(os.path.dirname(__file__), "assets", "NotoSansKR-Bold-subset.otf")


def _kr_font(size: int):
    from PIL import ImageFont
    try:
        return ImageFont.truetype(_KR_FONT_PATH, size)
    except Exception as e:
        print(f"  [WARN] 한글 폰트 로드 실패({e}), 기본 폰트로 대체")
        return ImageFont.load_default(size=size)


def _capture_dhlottery_result_image(result_url: str, round_no: int) -> bytes | None:
    """동행복권 추첨결과 페이지(로또645 /lt645/result, 연금복권720+ /pt720/result —
    둘 다 같은 마크업: .swiper-slide-active .result-infoWrap)에서 해당 회차
    결과 카드만 잘라 스크린샷. 사이트 개편·봇 차단·회차 표시 어긋남 등으로
    실패할 수 있어 무엇이든 잘못되면 예외를 삼키고 None을 반환한다 —
    호출부가 자체 생성 이미지로 대체한다(사용자 지시, 2026-08-31:
    "오류 나면 자체 생성으로")."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        print(f"  [WARN] playwright 미설치: {e}")
        return None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(
                    viewport={"width": 900, "height": 800},
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                               "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
                )
                page.goto(result_url, timeout=20000, wait_until="networkidle")
                el = page.locator(".swiper-slide-active .result-infoWrap")
                el.wait_for(state="visible", timeout=10000)
                if f"{round_no}회" not in el.inner_text():
                    print(f"  [WARN] 캡처 페이지 회차 불일치(기대 {round_no}회): {result_url}")
                    return None
                return el.screenshot()
            finally:
                browser.close()
    except Exception as e:
        print(f"  [WARN] 추첨결과 캡처 실패({result_url}): {e}")
        return None


def capture_lotto645_result_image(round_no: int) -> bytes | None:
    return _capture_dhlottery_result_image("https://www.dhlottery.co.kr/lt645/result", round_no)


def capture_pension720_result_image(round_no: int) -> bytes | None:
    return _capture_dhlottery_result_image("https://www.dhlottery.co.kr/pt720/result", round_no)


def generate_lotto645_ball_image(nums: list[int], bonus: int, round_no: int) -> bytes:
    """캡처 실패 시 대체용 자체 생성 이미지. 공 색상은 동행복권 공식 규칙과 동일."""
    import io
    from PIL import Image, ImageDraw

    W, H = 700, 220
    img = Image.new("RGB", (W, H), "#FFFFFF")
    draw = ImageDraw.Draw(img)
    title_font = _kr_font(26)
    small_font = _kr_font(15)
    num_font = _kr_font(24)

    draw.text((W / 2, 30), f"로또6/45 제{round_no}회 당첨번호", font=title_font, fill="#222222", anchor="mm")

    balls = [*nums, "+", bonus]
    r, gap = 28, 12
    total_w = len(balls) * (2 * r) + (len(balls) - 1) * gap
    x = (W - total_w) / 2 + r
    y = 120
    for b in balls:
        if b == "+":
            draw.text((x, y), "+", font=title_font, fill="#666666", anchor="mm")
        else:
            draw.ellipse((x - r, y - r, x + r, y + r), fill=_ball_color(b))
            draw.text((x, y), str(b), font=num_font, fill="white", anchor="mm")
        x += 2 * r + gap

    draw.text((W / 2, H - 25), "뉴스파이널", font=small_font, fill="#999999", anchor="mm")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def generate_pension720_ball_image(bnd: str, num: str, bonus_num: str, round_no: int) -> bytes:
    """캡처 실패 시 대체용 자체 생성 이미지."""
    import io
    from PIL import Image, ImageDraw

    W, H = 700, 260
    img = Image.new("RGB", (W, H), "#FFFFFF")
    draw = ImageDraw.Draw(img)
    title_font = _kr_font(24)
    label_font = _kr_font(15)
    num_font = _kr_font(20)

    draw.text((W / 2, 26), f"연금복권720+ 제{round_no}회 당첨번호", font=title_font, fill="#222222", anchor="mm")

    def draw_row(y: int, label: str, band: str, digits: str, color: str):
        draw.text((30, y), label, font=label_font, fill="#666666", anchor="lm")
        chars = ([band] if band else []) + list(digits)
        r, gap = 20, 8
        total_w = len(chars) * (2 * r) + (len(chars) - 1) * gap
        x = W - 30 - total_w + r
        for ch in chars:
            draw.ellipse((x - r, y - r, x + r, y + r), fill=color)
            draw.text((x, y), ch, font=num_font, fill="white", anchor="mm")
            x += 2 * r + gap

    draw_row(110, "1등", bnd, num, "#FF7272")
    draw_row(170, "보너스", "", bonus_num, "#69C8F2")

    draw.text((W / 2, H - 22), "뉴스파이널", font=label_font, fill="#999999", anchor="mm")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _get_dh_image_url(key_hint: str, capture_fn, generate_fn) -> str:
    """캡처 우선 시도, 실패하면 자체 생성 이미지로 대체 후 R2에 영구 저장."""
    data = capture_fn()
    if data is None:
        data = generate_fn()
    try:
        from image_store import store_image_bytes
        return store_image_bytes(data, "png", key_hint=key_hint)
    except Exception as e:
        print(f"  [WARN] 이미지 저장 실패: {e}")
        return ""


def _backfill_dh_image(url_key: str, label: str, get_image_url_fn) -> None:
    """이미 발행된 기사에 image_url이 비어 있으면 채워 넣는다.
    이미지 기능(2026-08-31) 추가 이전에 발행된 과거 기사 보정용."""
    res = requests.get(
        f"{_sb_articles_url()}?url=eq.{url_key}&select=id,image_url",
        headers=_sb_headers(), timeout=10,
    )
    if res.status_code not in (200, 206):
        return
    rows = res.json()
    if not rows or rows[0].get("image_url"):
        return
    image_url = get_image_url_fn()
    if not image_url:
        return
    patch = requests.patch(
        f"{_sb_articles_url()}?id=eq.{rows[0]['id']}",
        headers=_sb_headers(), json={"image_url": image_url}, timeout=15,
    )
    ok = patch.status_code in (200, 204)
    print(f"  {'🖼️' if ok else '✗'} {label} 이미지 백필: {'완료' if ok else '실패'}")


def get_lotto645_image_url(round_no: int, nums: list[int], bonus: int) -> str:
    return _get_dh_image_url(
        f"lotto645_{round_no}",
        lambda: capture_lotto645_result_image(round_no),
        lambda: generate_lotto645_ball_image(nums, bonus, round_no),
    )


def backfill_lotto645_image(round_no: int, nums: list[int], bonus: int) -> None:
    _backfill_dh_image(
        f"internal://lotto645_{round_no}", f"로또 {round_no}회",
        lambda: get_lotto645_image_url(round_no, nums, bonus),
    )


def get_pension720_image_url(round_no: int, bnd: str, num: str, bonus_num: str) -> str:
    return _get_dh_image_url(
        f"pension720_{round_no}",
        lambda: capture_pension720_result_image(round_no),
        lambda: generate_pension720_ball_image(bnd, num, bonus_num, round_no),
    )


def backfill_pension720_image(round_no: int, bnd: str, num: str, bonus_num: str) -> None:
    _backfill_dh_image(
        f"internal://pension720_{round_no}", f"연금복권 {round_no}회",
        lambda: get_pension720_image_url(round_no, bnd, num, bonus_num),
    )


def capture_powerball_result_image(draw_date_str: str) -> bytes | None:
    """powerball.com 결과 페이지에서 당첨번호+잭팟 카드만 잘라 스크린샷.
    dhlottery와 wait 전략이 다르다 — networkidle은 이 사이트에서 계속
    타임아웃(백그라운드 폴링으로 추정), domcontentloaded + 명시적 셀렉터
    대기가 안정적으로 확인됨(2026-08-31)."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        print(f"  [WARN] playwright 미설치: {e}")
        return None
    try:
        draw_date = datetime.strptime(draw_date_str, "%Y-%m-%d").date()
    except Exception:
        return None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(
                    viewport={"width": 900, "height": 800},
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                               "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
                )
                page.goto(f"https://www.powerball.com/draw-result?gc=powerball&date={draw_date_str}",
                          timeout=45000, wait_until="domcontentloaded")
                el = page.locator(".number-card.number-powerball")
                el.wait_for(state="visible", timeout=20000)
                expect = f"{draw_date.strftime('%b')} {draw_date.day}, {draw_date.year}"
                if expect not in el.inner_text():
                    print(f"  [WARN] 캡처 페이지 날짜 불일치(기대 {expect})")
                    return None
                return el.screenshot()
            finally:
                browser.close()
    except Exception as e:
        print(f"  [WARN] 파워볼 결과 캡처 실패: {e}")
        return None


def generate_powerball_ball_image(white_balls: list[int], powerball: int, draw_date, multiplier: str) -> bytes:
    """캡처 실패 시 대체용 자체 생성 이미지. 흰 공/빨간 파워볼 공 색상은
    공식 파워볼 표기와 동일."""
    import io
    from PIL import Image, ImageDraw

    W, H = 700, 220
    img = Image.new("RGB", (W, H), "#FFFFFF")
    draw = ImageDraw.Draw(img)
    title_font = _kr_font(24)
    small_font = _kr_font(15)
    num_font = _kr_font(22)

    draw.text((W / 2, 28), f"파워볼 {draw_date.month}월 {draw_date.day}일 당첨번호",
              font=title_font, fill="#222222", anchor="mm")

    balls = [*white_balls, powerball]
    r, gap = 26, 12
    total_w = len(balls) * (2 * r) + (len(balls) - 1) * gap
    x = (W - total_w) / 2 + r
    y = 115
    for i, b in enumerate(balls):
        is_pb = i == len(balls) - 1
        fill = "#D0021B" if is_pb else "#E5E5E5"
        text_color = "white" if is_pb else "#222222"
        draw.ellipse((x - r, y - r, x + r, y + r), fill=fill,
                     outline=None if is_pb else "#999999")
        draw.text((x, y), str(b), font=num_font, fill=text_color, anchor="mm")
        x += 2 * r + gap

    if multiplier:
        draw.text((W / 2, 165), f"파워플레이 {multiplier}배", font=small_font, fill="#666666", anchor="mm")

    draw.text((W / 2, H - 22), "뉴스파이널", font=small_font, fill="#999999", anchor="mm")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def get_powerball_image_url(draw_date_str: str, white_balls: list[int], powerball: int,
                             draw_date, multiplier: str) -> str:
    return _get_dh_image_url(
        f"powerball_{draw_date_str}",
        lambda: capture_powerball_result_image(draw_date_str),
        lambda: generate_powerball_ball_image(white_balls, powerball, draw_date, multiplier),
    )


def backfill_powerball_image(draw_date_str: str, white_balls: list[int], powerball: int,
                              draw_date, multiplier: str) -> None:
    _backfill_dh_image(
        f"internal://powerball_{draw_date_str}", f"파워볼 {draw_date_str}",
        lambda: get_powerball_image_url(draw_date_str, white_balls, powerball, draw_date, multiplier),
    )


def fetch_lotto645_winning_shops(round_no: int) -> list[dict] | None:
    """1등 당첨 판매점 목록 조회(지역 정보용). 실패 시 None(기사에서 해당 문장만 생략)."""
    try:
        res = requests.get(
            "https://www.dhlottery.co.kr/wnprchsplcsrch/selectLtWnShp.do",
            params={"srchWnShpRnk": "1", "srchLtEpsd": round_no, "srchShpLctn": ""},
            headers={"User-Agent": "Mozilla/5.0",
                     "Referer": "https://www.dhlottery.co.kr/wnprchsplcsrch/home"},
            timeout=15,
        )
        res.encoding = "utf-8"
        if res.status_code != 200:
            print(f"  [WARN] 당첨 판매점 조회 실패: HTTP {res.status_code}")
            return None
        return res.json().get("data", {}).get("list", [])
    except Exception as e:
        print(f"  [WARN] 당첨 판매점 조회 실패: {e}")
        return None


def _shop_region_summary(shops: list[dict]) -> str:
    """[{'region': '서울', ...}, ...] -> '서울 2곳, 부산 1곳, ...' (등장 순서 유지)"""
    counts: dict[str, int] = {}
    for shp in shops:
        region = (shp.get("region") or "").strip()
        if region:
            counts[region] = counts.get(region, 0) + 1
    return ", ".join(f"{region} {n}곳" for region, n in counts.items())


def build_lotto645_article(d: dict, shops: list[dict] | None = None) -> tuple[str, str, str]:
    nums = [d[f"tm{i}WnNo"] for i in range(1, 7)]
    bonus = d["bnsWnNo"]
    round_no = d["ltEpsd"]
    draw_date = datetime.strptime(d["ltRflYmd"], "%Y%m%d").date()

    nums_str = ", ".join(str(n) for n in nums)
    title = f"[복권] 로또 {round_no}회 당첨번호 {nums_str}…보너스 {bonus}"

    auto_n, manual_n, semi_n = d.get("winType1", 0), d.get("winType2", 0), d.get("winType3", 0)
    type_note = ""
    if auto_n + manual_n + semi_n == d["rnk1WnNope"]:
        type_note = f"(자동 {auto_n}명, 수동 {manual_n}명, 반자동 {semi_n}명)"

    shop_sentence = ""
    if shops:
        region_summary = _shop_region_summary(shops)
        if region_summary:
            shop_sentence = f" 1등 당첨 판매점은 {region_summary}에서 나왔다."

    body = (
        f"동행복권이 {draw_date.day}일 로또6/45 {round_no}회 당첨번호를 추첨·발표했다. "
        f"당첨번호는 {nums_str}이며 보너스 번호는 {bonus}다.\n\n"
        f"1등 당첨자는 {format_count(d['rnk1WnNope'])}명{type_note}으로 각자 {format_amount(d['rnk1WnAmt'])}씩 받는다. "
        f"1등 총 당첨금은 {format_amount(d['rnk1SumWnAmt'])}이다.{shop_sentence}\n"
        f"2등 당첨자는 {format_count(d['rnk2WnNope'])}명으로 각자 {format_amount(d['rnk2WnAmt'])}씩 받는다.\n"
        f"3등 당첨자는 {format_count(d['rnk3WnNope'])}명으로 각자 {format_amount(d['rnk3WnAmt'])}씩 받는다.\n"
        f"4등(5개 번호 일치)은 {format_count(d['rnk4WnNope'])}명으로 각자 {format_amount(d['rnk4WnAmt'])}씩, "
        f"5등(4개 번호 일치)은 {format_count(d['rnk5WnNope'])}명으로 각자 {format_amount(d['rnk5WnAmt'])}씩 받는다.\n\n"
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
    # 2026-09-03 실사고(id=126890): 일시적 네트워크 오류로 이 호출이 빈 리스트를
    # 반환하면 build_pension720_article()의 모든 등수 단락이 통째로 스킵돼
    # "…추첨·발표했다." 한 문장짜리 기사가 나갔다(재호출 시 API 자체는 정상
    # 응답 확인됨 — 일시적 문제였음). 재시도 1회 추가.
    for attempt in range(2):
        try:
            res = requests.get(
                "https://www.dhlottery.co.kr/pt720/selectPstPt720WnInfo.do",
                params={"srchPsltEpsd": round_no},
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.dhlottery.co.kr/pt720/result"},
                timeout=15,
            )
            if res.status_code != 200:
                if attempt == 0:
                    time.sleep(3)
                    continue
                return []
            result = res.json().get("data", {}).get("result", [])
            if not result and attempt == 0:
                time.sleep(3)
                continue
            return result
        except Exception as e:
            print(f"  [WARN] 연금복권 당첨금 조회 실패(시도 {attempt+1}): {e}")
            if attempt == 0:
                time.sleep(3)
                continue
            return []
    return []


# 연금복권720+ 등수별 당첨금은 매 회차 고정이다(사용자 확인, 2026-08-21).
# API의 wnAmt/totAmt 필드 의미가 불명확해서(예: 3등 wnAmt=3,700만원인데
# 실제 고정 당첨금은 100만원 — 다른 걸 가리키는 필드로 추정) 이 필드들을
# 아예 쓰지 않고, 공식 발표 고정액을 그대로 상수로 박아둔다. API에서는
# 회차마다 변하는 "당첨자 수"(wnTotalCnt)만 가져온다 — 금액을 API 필드
# 해석에 의존하지 않아야 오탐 위험이 없다(사용자 요구: "절대 오타 금지").
#
# 각 필드: (라벨, 세전 월지급액 또는 None(월지급식이 아닌 등수), 지급개월수, 고정 당첨금 문구)
_PT720_RANK_INFO = {
    1: ("1등", 7_000_000, 20 * 12, "매월 700만원씩 20년(세전 총 16억 8000만원)"),
    2: ("2등", 1_000_000, 10 * 12, "매월 100만원씩 10년(세전 총 1억 2000만원)"),
    3: ("3등", None, None, "100만원"),
    4: ("4등", None, None, "10만원"),
    5: ("5등", None, None, "5만원"),
    6: ("6등", None, None, "5000원"),
    7: ("7등", None, None, "1000원"),
    8: ("보너스", 1_000_000, 10 * 12, "매월 100만원씩 10년(세전 총 1억 2000만원)"),
}

# 복권 당첨금 기타소득세(3억원 이하 구간): 소득세 20% + 지방소득세 2% = 22%.
# 연금복권 월 지급액(700만원/100만원)은 회차당 지급액 기준이라 이 구간에
# 항상 해당한다(사용자 요구: 세율은 세법에 명시된 고정값만 쓰고 추정 금지).
_PT720_TAX_RATE = 0.22


def _after_tax_monthly(monthly_krw: int) -> str:
    net = round(monthly_krw * (1 - _PT720_TAX_RATE))
    return f"{net // 10_000}만원"


# 하위 등수 당첨번호는 당첨번호 끝자리를 그대로 쓴다(연금복권 공식 규정 —
# 등수가 내려갈수록 뒷자리 일치 개수가 하나씩 줄어든다). 이미 확보한 num
# 문자열을 슬라이싱하기만 하므로 API 재호출·추정 없이 오타 위험이 없다.
_PT720_RANK_DIGITS = {2: 6, 3: 5, 4: 4, 5: 3, 6: 2, 7: 1}


def build_pension720_article(latest: dict, prizes: list) -> tuple[str, str, str]:
    round_no = latest["psltEpsd"]
    draw_date = datetime.strptime(latest["psltRflYmd"], "%Y%m%d").date()
    bnd = latest["wnBndNo"]
    num = str(latest["wnRnkVl"])
    bonus_num = latest["bnsRnkVl"]

    title = f"[복권] 연금복권720+ {round_no}회 당첨번호 {bnd}조 {num}"

    by_rank = {p["wnRnk"]: p for p in prizes}

    def cnt(rank):
        p = by_rank.get(rank)
        return p["wnTotalCnt"] if p and p.get("wnTotalCnt") else None

    paras = [f"동행복권이 {draw_date.day}일 연금복권720+ {round_no}회 당첨번호를 추첨·발표했다."]

    c1 = cnt(1)
    if c1:
        paras.append(f"{round_no}회 연금복권720+의 1등 당첨번호는 '{bnd}조 {num}'이다.")
        paras.append(
            f"1등 당첨자는 {format_count(c1)}명이다. 1등에 당첨될 시 매달 700만원씩 20년간 연금식으로 받게 된다. "
            f"세후 실수령액은 월 약 {_after_tax_monthly(_PT720_RANK_INFO[1][1])}이다."
        )

    c2 = cnt(2)
    if c2:
        paras.append(f"2등 당첨번호는 '{num}'이다.")
        paras.append(f"당첨자는 {format_count(c2)}명이다. 2등에 당첨될 경우 월 100만원을 10년간 연금식으로 받는다.")

    for rank, unit in ((3, "100만원"), (4, "10만원"), (5, "5만원"), (6, "5,000원"), (7, "1,000원")):
        c = cnt(rank)
        if not c:
            continue
        digits = num[-_PT720_RANK_DIGITS[rank]:]
        paras.append(f"{rank}등 당첨번호는 '{digits}'이다. 당첨자 총 {format_count(c)}명에게 각 {unit}이 지급된다.")

    c8 = cnt(8)
    if c8:
        paras.append(f"보너스 번호는 각 조 '{bonus_num}'이다.")
        paras.append(
            f"보너스 당첨자는 {format_count(c8)}명이다. 보너스 번호 6자리가 일치한 당첨자는 월 100만원씩 10년간 "
            f"연금식으로 지급받는다."
        )

    body = "\n\n".join(paras)
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


# data.ny.gov(공식 소스)는 당첨번호만 제공하고 잭팟 금액·등수별 당첨자 수는
# 없다(2026-08-21 확인). powerball.com에 별도 공개 API는 없지만(2026-08-31
# 확인, 네트워크 요청 전부가 서버렌더 HTML) 결과 페이지 DOM 구조가 안정적
# (등수별 로또볼에 m5-pb/m5/m4-pb/... 클래스가 붙어 있어 순서가 아니라
# 클래스명으로 등수를 식별할 수 있음)이라 스크레이핑으로 보강한다.
_PB_TIER_LABELS = {
    "m5-pb": "1등(파워볼 포함 5개 일치, 잭팟)",
    "m5": "2등(5개 일치)",
    "m4-pb": "3등(파워볼 포함 4개 일치)",
    "m4": "4등(4개 일치)",
    "m3-pb": "5등(파워볼 포함 3개 일치)",
    "m3": "6등(3개 일치)",
    "m2-pb": "7등(파워볼 포함 2개 일치)",
    "m1-pb": "8등(파워볼 포함 1개 일치)",
    "m0-pb": "9등(파워볼만 일치)",
}


def _usd_amount_to_kr(text: str) -> str:
    """'$1,000,000' -> '100만 달러', '$50,000' -> '5만 달러', '$100' -> '100달러'.
    2026-09-03 사용자 지적: "$1,000,000 이러면 가시성이 떨어지잖아. 100만달러라고
    적어야지" — 원화 억/만 표시 규칙(feedback_korean_won_man_unit_format)을
    달러 등수별 상금에도 동일 적용."""
    import re as _re
    m = _re.search(r"[\d,]+", text or "")
    if not m:
        return text or ""
    n = int(m.group(0).replace(",", ""))
    if n < 10_000:
        return f"{n:,}달러"
    eok, rem = divmod(n, 100_000_000)
    man, _rem2 = divmod(rem, 10_000)
    parts = []
    if eok:
        parts.append(f"{eok}억")
    if man:
        parts.append(f"{man}만")
    return " ".join(parts) + " 달러"


def _usd_million_to_kr(million: float) -> str:
    """119.0 -> '1억 1900만 달러' (억/만 단위는 format_amount와 동일한 방식)."""
    total_man = round(million * 100)
    eok, man = divmod(total_man, 10_000)
    parts = []
    if eok:
        parts.append(f"{eok}억")
    if man or not parts:
        parts.append(f"{man}만")
    return " ".join(parts) + " 달러"


def fetch_powerball_prize_data(draw_date_str: str) -> dict | None:
    """powerball.com 결과 페이지에서 잭팟 금액·현금가치·등수별 당첨자 수를 가져온다.
    페이지 구조가 바뀌는 등 무엇이든 실패하면 None — 호출부는 당첨번호만으로 발행한다."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        print(f"  [WARN] playwright 미설치: {e}")
        return None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                               "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
                )
                page.goto(f"https://www.powerball.com/draw-result?gc=powerball&date={draw_date_str}",
                          timeout=30000, wait_until="load")
                page.wait_for_selector("table tbody tr", timeout=15000)
                data = page.evaluate("""() => {
                    const table = document.querySelector('table');
                    const rows = table ? [...table.querySelectorAll('tbody tr')] : [];
                    const tiers = rows.map(r => {
                        const matchCell = r.querySelector('.match-col');
                        const ballsDiv = matchCell ? matchCell.querySelector('.game-balls') : null;
                        const tierClass = ballsDiv ? [...ballsDiv.classList].find(c => c.startsWith('m')) : null;
                        const tds = [...r.querySelectorAll('td')].map(td => td.textContent.replace(/\\s+/g,' ').trim());
                        return [tierClass, tds];
                    });
                    const jackpotEl = document.querySelector('.estimated-jackpot');
                    const cashEl = document.querySelector('.cash-value');
                    const card = document.querySelector('.winner-card.winner-powerball');
                    const groups = card
                        ? [...card.querySelectorAll('.winners-group')].map(g => g.textContent.replace(/\\s+/g,' ').trim())
                        : [];
                    return {
                        jackpot_text: jackpotEl ? jackpotEl.textContent.replace(/\\s+/g,' ').trim() : null,
                        cash_text: cashEl ? cashEl.textContent.replace(/\\s+/g,' ').trim() : null,
                        groups: groups,
                        tiers: tiers,
                    };
                }""")
                if not data or not data.get("tiers"):
                    return None
                return data
            finally:
                browser.close()
    except Exception as e:
        print(f"  [WARN] 파워볼 상금 정보 조회 실패: {e}")
        return None


def _pb_prize_sentences(prize: dict) -> str:
    """스크레이핑 결과(dict)를 기사 문단으로 조립. 파싱 중 무엇이든 안 맞으면
    빈 문자열을 돌려줘 기존 당첨번호만 있는 기사로 안전하게 후퇴한다."""
    try:
        import re as _re
        tiers = {t[0]: t[1] for t in prize["tiers"] if t[0]}

        m = _re.search(r"([\d.]+)\s*Million", prize.get("jackpot_text") or "", _re.I)
        jackpot_kr = _usd_million_to_kr(float(m.group(1))) if m else None
        m = _re.search(r"([\d.]+)\s*Million", prize.get("cash_text") or "", _re.I)
        cash_kr = _usd_million_to_kr(float(m.group(1))) if m else None
        if not (jackpot_kr and cash_kr):
            return ""

        groups = prize.get("groups") or []
        jackpot_line = groups[0] if len(groups) > 0 else ""
        match5_line = groups[2] if len(groups) > 2 else ""

        # 2026-09-03 사용자 지적: "잭팟이 1억5000만달러인데 현금가치 6520만달러?
        # 이해를 못하겠는데" — 파워볼 잭팟은 29년 연금 분할 지급 기준 총액이고,
        # 일시불(현금가치)은 그 미래 지급액을 현재가치로 할인한 금액이라 항상
        # 더 작다(금리에 따라 대략 잭팟의 45~65% 수준). 숫자만 나열하면 오해하기
        # 쉬워 한 줄로 설명을 덧붙인다.
        sentences = [
            f"이번 추첨의 추정 잭팟은 {jackpot_kr}(현금가치 {cash_kr})였다.",
            "잭팟 금액은 29년에 걸쳐 나눠 받는 연금 지급 기준 총액이고, "
            "현금가치는 당첨자가 한 번에 일시불로 받을 경우 받는 금액이라 잭팟보다 작다.",
        ]

        if "none" in jackpot_line.lower():
            sentences.append("이번 추첨에서 1등(잭팟) 당첨자는 나오지 않았다.")
        else:
            states = jackpot_line.split("None")[-1].replace("Powerball JACKPOT WINNERS", "").strip()
            sentences.append("이번 추첨에서 1등(잭팟) 당첨자가 나왔다.")
            if states:
                sentences.append(f"당첨 지역은 {states}다.")

        m5 = tiers.get("m5")
        if m5 and len(m5) >= 3:
            m5_count = format_count(int(m5[1].replace(",", "")))
            states = match5_line.split("Winners")[-1].strip()
            sentences.append(f"2등(5개 번호 일치)은 {m5_count}명으로 각자 {_usd_amount_to_kr(m5[2])}를 받는다.")
            if states and "none" not in states.lower():
                sentences.append(f"2등 당첨 지역은 {states}다.")

        etc_labels = ["m4-pb", "m4", "m3-pb", "m3", "m2-pb", "m1-pb", "m0-pb"]
        etc_parts = []
        for key in etc_labels:
            row = tiers.get(key)
            if not row or len(row) < 3:
                continue
            count = format_count(int(row[1].replace(",", "")))
            etc_parts.append(f"{_PB_TIER_LABELS[key]} {count}명({_usd_amount_to_kr(row[2])})")
        if etc_parts:
            sentences.append("이 밖에 " + ", ".join(etc_parts) + "이 당첨됐다.")

        return " ".join(sentences)
    except Exception as e:
        print(f"  [WARN] 파워볼 상금 정보 파싱 실패: {e}")
        return ""


def build_powerball_article(d: dict, prize: dict | None = None) -> tuple[str, str, str]:
    draw_date = datetime.strptime(d["draw_date"][:10], "%Y-%m-%d").date()
    nums = [int(n) for n in d["winning_numbers"].split()]
    white_balls, powerball = nums[:5], nums[5]
    multiplier = d.get("multiplier", "")

    nums_str = ", ".join(str(n) for n in white_balls)
    title = f"[복권] 美 파워볼 {draw_date.month}월 {draw_date.day}일 당첨번호 {nums_str}+파워볼 {powerball}"

    prize_text = _pb_prize_sentences(prize) if prize else ""

    body = (
        f"미국 파워볼이 {draw_date.month}월 {draw_date.day}일(현지시간) 추첨한 당첨번호를 발표했다. "
        f"당첨번호는 {nums_str}이며 파워볼 번호는 {powerball}이다."
        + (f" 이날 파워플레이 배수는 {multiplier}배였다." if multiplier else "")
        + (f"\n\n{prize_text}" if prize_text else "")
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
        d = fetch_lotto645(round_no)
        if d:
            nums = [d[f"tm{i}WnNo"] for i in range(1, 7)]
            backfill_lotto645_image(round_no, nums, d["bnsWnNo"])
    else:
        d = fetch_lotto645(round_no)
        if d:
            shops = fetch_lotto645_winning_shops(round_no)
            title, body, url_key = build_lotto645_article(d, shops)
            nums = [d[f"tm{i}WnNo"] for i in range(1, 7)]
            image_url = get_lotto645_image_url(round_no, nums, d["bnsWnNo"])
            aid = insert_article(title, body, url_key, countries=["한국"], image_url=image_url)
            print(f"  {'✓' if aid > 0 else '✗'} 로또 {round_no}회: id={aid}")
        else:
            print(f"  → 로또 {round_no}회 데이터 없음(아직 추첨 전이거나 회차 계산 오류)")

    # 연금복권720+
    latest = fetch_pension720_latest()
    if latest:
        p_round = latest["psltEpsd"]
        p_url = f"internal://pension720_{p_round}"
        p_bnd = latest["wnBndNo"]
        p_num = str(latest["wnRnkVl"])
        p_bonus_num = latest["bnsRnkVl"]
        if already_published(p_url):
            print(f"  → 연금복권 {p_round}회 이미 발행됨 → 스킵")
            backfill_pension720_image(p_round, p_bnd, p_num, p_bonus_num)
        else:
            prizes = fetch_pension720_prizes(p_round)
            title, body, url_key = build_pension720_article(latest, prizes)
            image_url = get_pension720_image_url(p_round, p_bnd, p_num, p_bonus_num)
            aid = insert_article(title, body, url_key, countries=["한국"], image_url=image_url)
            print(f"  {'✓' if aid > 0 else '✗'} 연금복권 {p_round}회: id={aid}")
    else:
        print("  → 연금복권 데이터 조회 실패")

    # 미국 파워볼
    pb = fetch_powerball_latest()
    if pb:
        pb_date = pb["draw_date"][:10]
        pb_url = f"internal://powerball_{pb_date}"
        pb_nums = [int(n) for n in pb["winning_numbers"].split()]
        pb_white, pb_ball = pb_nums[:5], pb_nums[5]
        pb_draw_date = datetime.strptime(pb_date, "%Y-%m-%d").date()
        pb_multiplier = pb.get("multiplier", "")
        if already_published(pb_url):
            print(f"  → 파워볼 {pb_date} 이미 발행됨 → 스킵")
            backfill_powerball_image(pb_date, pb_white, pb_ball, pb_draw_date, pb_multiplier)
        else:
            prize = fetch_powerball_prize_data(pb_date)
            title, body, url_key = build_powerball_article(pb, prize)
            image_url = get_powerball_image_url(pb_date, pb_white, pb_ball, pb_draw_date, pb_multiplier)
            aid = insert_article(title, body, url_key, countries=["미국"], image_url=image_url)
            print(f"  {'✓' if aid > 0 else '✗'} 파워볼 {pb_date}: id={aid}")
    else:
        print("  → 파워볼 데이터 조회 실패")

    print(f"[lotto_writer] 완료: {now_kst().strftime('%Y-%m-%d %H:%M')} KST")


if __name__ == "__main__":
    run()
