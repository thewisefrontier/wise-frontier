import sys
import os
import time
import requests
from dotenv import load_dotenv

sys.stdout.reconfigure(encoding='utf-8')
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = "@NewsFinalKR"

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

def _sb_headers():
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
    }

REGION_EMOJI = {
    "africa": "🌍", "southeast_asia": "🌏", "eastern_europe": "🌐",
    "central_asia": "🏔️", "middle_east": "🌙", "south_asia": "🌏",
    "caribbean": "🌴", "latin_america": "🌎", "global": "🗺️"
}

REGION_NAME = {
    "africa": "아프리카", "southeast_asia": "동남아시아",
    "eastern_europe": "동유럽", "central_asia": "중앙아시아",
    "middle_east": "중동", "south_asia": "남아시아",
    "caribbean": "카리브해", "latin_america": "라틴아메리카",
    "global": "글로벌"
}

def get_today_articles(date: str, limit: int = 20) -> list:
    res = requests.get(
        f"{SUPABASE_URL}/rest/v1/articles",
        headers=_sb_headers(),
        params={
            "select": "id,title_ko,title_en,url,source,category,region,country,country_flag,created_at",
            "source": "eq.NewsFinal",
            "created_at": f"like.{date}%",
            "order": "created_at.desc",
            "limit": str(limit),
        },
        timeout=30
    )
    if res.status_code in (200, 206):
        return res.json()
    return []

def send_telegram(msg: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    res = requests.post(url, data={
        "chat_id": CHAT_ID,
        "text": msg,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True
    })
    return res.json()

def build_briefing(date: str = None) -> str:
    if date is None:
        date = time.strftime("%Y-%m-%d")

    articles = get_today_articles(date=date, limit=20)
    if not articles:
        return ""

    date_str = time.strftime("%Y년 %m월 %d일")
    lines = [
        f"📰 *NewsFinal — {date_str} 브리핑*",
        f"오늘의 프론티어 마켓 주요 뉴스입니다.\n"
    ]

    # 지역별 분류
    by_region = {}
    for a in articles:
        r = a.get("region", "global") or "global"
        if r not in by_region:
            by_region[r] = []
        by_region[r].append(a)

    for region, items in by_region.items():
        emoji = REGION_EMOJI.get(region, "🗺️")
        name = REGION_NAME.get(region, region)
        lines.append(f"\n{emoji} *{name}*")

        for a in items:
            country_flag = a.get("country_flag", "")
            country = a.get("country", "")
            title_ko = a.get("title_ko") or a.get("title_en", "")
            # 자체 기사는 사이트 링크로
            url = f"https://newsfinal.co.kr/article.html?id={a.get('id')}"
            country_str = f" {country_flag} {country}" if country_flag else ""
            lines.append(f"📌{country_str} [{title_ko}]({url})")

    lines.append(f"\n_NewsFinal | 프론티어 미디어_")
    return "\n".join(lines)

if __name__ == "__main__":
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("[ERROR] SUPABASE_URL 또는 SUPABASE_SERVICE_KEY 없음")
        exit(1)

    date = time.strftime("%Y-%m-%d")
    print(f"[브리핑] {date} 브리핑 생성 중...")

    briefing = build_briefing(date)
    if not briefing:
        print("[브리핑] 오늘 기사가 없습니다.")
        exit(0)

    print(briefing)
    print("\n텔레그램 발송 중...")
    res = send_telegram(briefing)

    if res.get("ok"):
        print("✅ 브리핑 발송 완료!")
    else:
        print(f"❌ 발송 실패: {res}")
