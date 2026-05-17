import sys
import os
import time
import requests
from dotenv import load_dotenv
from db import init_db, get_top_articles, get_articles_by_region

sys.stdout.reconfigure(encoding='utf-8')
load_dotenv()

# =========================
# CONFIG
# =========================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = "@TheWiseFrontier"

# =========================
# REGION 설정
# =========================

REGION_EMOJI = {
    "africa":         "🌍",
    "southeast_asia": "🌏",
    "eastern_europe": "🌐",
    "central_asia":   "🏔️",
    "middle_east":    "🌙",
    "south_asia":     "🌏",
    "caribbean":      "🌴",
    "latin_america":  "🌎",
    "global":         "🗺️"
}

REGION_NAME = {
    "africa":         "아프리카",
    "southeast_asia": "동남아시아",
    "eastern_europe": "동유럽",
    "central_asia":   "중앙아시아",
    "middle_east":    "중동",
    "south_asia":     "남아시아",
    "caribbean":      "카리브해",
    "latin_america":  "라틴아메리카",
    "global":         "글로벌"
}

CATEGORY_EMOJI = {
    "macro":      "📊",
    "finance":    "💰",
    "resource":   "⛏️",
    "politics":   "🏛️",
    "tech":       "💡",
    "society":    "🏘️",
    "investment": "📈"
}

# =========================
# TELEGRAM
# =========================

def send_telegram(msg: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    res = requests.post(url, data={
        "chat_id":    CHAT_ID,
        "text":       msg,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True
    })
    return res.json()

# =========================
# 브리핑 생성
# =========================

def build_briefing(date: str = None) -> str:
    if date is None:
        date = time.strftime("%Y-%m-%d")

    articles = get_top_articles(date=date, limit=15)

    if not articles:
        return ""

    # 날짜 포맷
    date_str = time.strftime("%Y년 %m월 %d일")

    lines = [
        f"📰 *더 와이즈 프론티어 — {date_str} 브리핑*",
        f"오늘의 프론티어 마켓 주요 뉴스입니다.\n"
    ]

    # 지역별로 분류
    by_region = {}
    for a in articles:
        r = a.get("region", "global")
        if r not in by_region:
            by_region[r] = []
        by_region[r].append(a)

    for region, items in by_region.items():
        emoji = REGION_EMOJI.get(region, "🗺️")
        name  = REGION_NAME.get(region, region)
        lines.append(f"\n{emoji} *{name}*")

        for a in items:
            cat_emoji    = CATEGORY_EMOJI.get(a.get("category", ""), "📌")
            country_flag = a.get("country_flag", "")
            title_ko     = a.get("title_ko") or a.get("title_en", "")
            url          = a.get("url", "")
            country      = a.get("country", "")

            country_str = f" {country_flag} {country}" if country_flag else ""
            lines.append(f"{cat_emoji}{country_str} [{title_ko}]({url})")

    lines.append(f"\n_더 와이즈 프론티어 | 프론티어 마켓 전문 미디어_")

    return "\n".join(lines)

# =========================
# MAIN
# =========================

if __name__ == "__main__":
    init_db()

    date = time.strftime("%Y-%m-%d")
    print(f"[브리핑] {date} 브리핑 생성 중...")

    briefing = build_briefing(date)

    if not briefing:
        print("[브리핑] 오늘 발송된 기사가 없습니다.")
        exit(0)

    print(briefing)
    print("\n텔레그램 발송 중...")

    res = send_telegram(briefing)

    if res.get("ok"):
        print("✅ 브리핑 발송 완료!")
    else:
        print(f"❌ 발송 실패: {res}")
