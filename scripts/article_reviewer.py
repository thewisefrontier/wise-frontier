"""
scripts/article_reviewer.py
---------------------------
발행된 자체기사(source='NewsFinal')를 정기 스캔해 편집 원칙 위반을 검수한다.

- 합쇼체(-습니다/-입니다) : 자동 변환 + update_log 기록
- 절대날짜 / 타 매체명 / 기자명·특파원 / (현지시간) 누락 : 플래그 + 관리자 알림

공통 로직은 gemini_writer.py를 import해 재사용한다
(시스템_아키텍처_현황.md 5장 "신규 모듈 작성 패턴" 참조).

제외 정책
- category='날씨' : 전면 제외. 합쇼체 81건의 톤 정책이 미결이고,
  한국 날씨 3함수는 "(현지시간)" 미표기가 정상이라 날짜 검수도 오탐이 된다.
- category='다이제스트','브리핑' : 여러 기사를 묶는 형식이라 (현지시간) 검사만 제외.

실행: python scripts/article_reviewer.py
"""

import os
import re
import requests
from datetime import timedelta

from gemini_writer import (
    has_polite_ending,
    to_plain_style,
    _sb_headers,
    _sb_url,
    SUPABASE_URL,
    now_kst,
)

SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_ADMIN_CHAT_ID = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "")

# ── 설정 (env로 조정 가능) ────────────────────────────────────────────
CHECK_WINDOW_HOURS = int(os.getenv("REVIEW_WINDOW_HOURS", "48"))
FETCH_LIMIT = int(os.getenv("REVIEW_FETCH_LIMIT", "500"))
ALERT_LIMIT = int(os.getenv("REVIEW_ALERT_LIMIT", "15"))
AUTO_FIX_POLITE = os.getenv("REVIEW_AUTO_FIX_POLITE", "1") != "0"
DRY_RUN = os.getenv("REVIEW_DRY_RUN", "0") == "1"

# 검수 전면 제외 카테고리
SKIP_CATEGORIES = {"날씨"}
# (현지시간) 검사만 제외하는 카테고리
DATE_EXEMPT_CATEGORIES = {"다이제스트", "브리핑"}

# ── 감지 패턴 ────────────────────────────────────────────────────────
# 절대날짜: "2026년 7월 15일" 형식. 날짜는 "N일(현지시간)"으로만 표기해야 한다.
ABS_DATE_RE = re.compile(r"\d{4}\s*년\s*\d{1,2}\s*월\s*\d{1,2}\s*일")

# 타 매체명 언급 금지
MEDIA_NAMES = [
    "로이터", "AFP", "AP통신", "블룸버그", "신화통신", "타스통신", "타스",
    "알자지라", "가디언", "뉴욕타임스", "워싱턴포스트", "월스트리트저널",
    "파이낸셜타임스", "닛케이", "니혼게이자이", "BBC", "CNN", "CNBC",
    "연합뉴스", "뉴시스", "조선일보", "중앙일보", "동아일보", "한겨레",
]
MEDIA_RE = re.compile("|".join(re.escape(m) for m in MEDIA_NAMES))

# 기자명·특파원 등 원문 바이라인 잔재
REPORTER_RE = re.compile(
    r"기자[가는이]\s*[^.\n]{0,40}?(보도|전했|밝혔|썼다)"
    r"|특파원"
    r"|본지\s*(취재|보도)"
)

LOCAL_TIME_TOKEN = "현지시간"


def fetch_candidates() -> list:
    """최근 CHECK_WINDOW_HOURS 시간 내 발행된 자체기사 조회.

    created_at은 16자 KST 텍스트("YYYY-MM-DD HH:MM")라 문자열 비교로 필터한다.
    """
    since = (now_kst() - timedelta(hours=CHECK_WINDOW_HOURS)).strftime("%Y-%m-%d %H:%M")
    try:
        res = requests.get(
            _sb_url(),
            headers=_sb_headers(),
            params={
                "select": "id,title_ko,summary_ko,category,update_log,created_at",
                "source": "eq.NewsFinal",
                "is_published": "eq.true",
                "created_at": f"gte.{since}",
                "limit": str(FETCH_LIMIT),
            },
            timeout=30,
        )
        if res.status_code in (200, 206):
            return res.json()
        print(f"[ERROR] 기사 조회 실패: HTTP {res.status_code} {res.text[:200]}")
    except Exception as e:
        print(f"[ERROR] 기사 조회 실패: {e}")
    return []


def detect_flags(title: str, body: str, category: str) -> list:
    """자동 수정하지 않고 알림만 보내는 위반 항목 탐지."""
    flags = []
    joined = f"{title}\n{body}"

    if ABS_DATE_RE.search(joined):
        m = ABS_DATE_RE.search(joined)
        flags.append(f"절대날짜({m.group(0)})")

    m = MEDIA_RE.search(joined)
    if m:
        flags.append(f"타매체명({m.group(0)})")

    m = REPORTER_RE.search(joined)
    if m:
        flags.append(f"기자명({m.group(0)[:20]})")

    if (category or "") not in DATE_EXEMPT_CATEGORIES and LOCAL_TIME_TOKEN not in joined:
        flags.append("현지시간 누락")

    return flags


def apply_polite_fix(title: str, body: str):
    """합쇼체 종결을 해라체로 변환. 변경분이 있으면 (새제목, 새본문, 변경필드) 반환."""
    new_title, new_body = title or "", body or ""
    changed = []

    if has_polite_ending(new_title):
        fixed = to_plain_style(new_title)
        if fixed != new_title:
            new_title = fixed
            changed.append("title_ko")

    if has_polite_ending(new_body):
        fixed = to_plain_style(new_body)
        if fixed != new_body:
            new_body = fixed
            changed.append("summary_ko")

    return new_title, new_body, changed


def patch_article(article_id: int, fields: dict, existing_log, note: str) -> bool:
    payload = dict(fields)
    payload["update_log"] = (existing_log or []) + [{
        "timestamp": now_kst().strftime("%Y-%m-%d %H:%M"),
        "note": note,
    }]
    if DRY_RUN:
        print(f"  [DRY-RUN] id={article_id} 패치 생략 — {note}")
        return True
    try:
        res = requests.patch(
            f"{_sb_url()}?id=eq.{article_id}",
            headers=_sb_headers(),
            json=payload,
            timeout=15,
        )
        if res.status_code in (200, 204):
            return True
        print(f"[ERROR] id={article_id} 업데이트 실패: HTTP {res.status_code} {res.text[:200]}")
    except Exception as e:
        print(f"[ERROR] id={article_id} 업데이트 실패: {e}")
    return False


def send_telegram_alert(fixed: list, flagged: list, scanned: int):
    if not TELEGRAM_TOKEN or not TELEGRAM_ADMIN_CHAT_ID:
        return
    if not fixed and not flagged:
        return

    lines = [f"📝 기사 검수 리포트 (최근 {CHECK_WINDOW_HOURS}시간 / {scanned}건 스캔)"]

    if fixed:
        lines.append(f"\n🔧 합쇼체 자동 변환 {len(fixed)}건")
        for item in fixed[:ALERT_LIMIT]:
            lines.append(f"- id={item['id']} {item['title'][:35]}")
        if len(fixed) > ALERT_LIMIT:
            lines.append(f"...외 {len(fixed) - ALERT_LIMIT}건")

    if flagged:
        lines.append(f"\n⚠️ 확인 필요 {len(flagged)}건")
        for item in flagged[:ALERT_LIMIT]:
            lines.append(f"- id={item['id']} [{', '.join(item['flags'])}] {item['title'][:30]}")
        if len(flagged) > ALERT_LIMIT:
            lines.append(f"...외 {len(flagged) - ALERT_LIMIT}건")

    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_ADMIN_CHAT_ID, "text": "\n".join(lines)},
            timeout=15,
        )
    except Exception as e:
        print(f"[WARN] 텔레그램 알림 실패: {e}")


def run():
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("[SKIP] Supabase 설정 없음")
        return

    articles = fetch_candidates()
    targets = [a for a in articles if (a.get("category") or "") not in SKIP_CATEGORIES]
    skipped = len(articles) - len(targets)
    print(f"[기사 검수] 최근 {CHECK_WINDOW_HOURS}시간 {len(articles)}건 조회 "
          f"(제외 {skipped}건) → {len(targets)}건 스캔")

    fixed, flagged = [], []

    for a in targets:
        aid = a.get("id")
        title = a.get("title_ko") or ""
        body = a.get("summary_ko") or ""
        category = a.get("category") or ""

        if AUTO_FIX_POLITE:
            new_title, new_body, changed = apply_polite_fix(title, body)
            if changed:
                payload = {}
                if "title_ko" in changed:
                    payload["title_ko"] = new_title
                if "summary_ko" in changed:
                    payload["summary_ko"] = new_body
                if patch_article(aid, payload, a.get("update_log"),
                                 f"합쇼체 자동 변환({', '.join(changed)})"):
                    title, body = new_title, new_body
                    fixed.append({"id": aid, "title": title})
                    print(f"  ✅ id={aid} 합쇼체 변환 — {', '.join(changed)}")

        flags = detect_flags(title, body, category)
        if flags:
            flagged.append({"id": aid, "title": title, "flags": flags})
            print(f"  ⚠️ id={aid} {', '.join(flags)}")

    send_telegram_alert(fixed, flagged, len(targets))
    print(f"[기사 검수] 완료 — 자동변환 {len(fixed)}건, 확인필요 {len(flagged)}건")


if __name__ == "__main__":
    run()
