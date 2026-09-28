# -*- coding: utf-8 -*-
"""
scripts/disclosure_collector.py
-------------------------------
거래소 공시 중 '중요 공시'만 골라 raw_candidates에 넣는 수집기 (2026-09-28).

사용자 지시: "믿을 수 있는 소스(거래소 공지·공시)는 그냥 기사로 낸다" + "주요 공시만 필터링해서 필요한 것만 가져와야지.
안 그러면 DB 터진다". → 공시 전량을 저장하지 않는다. 수집 단계에서 ① 중요도 등급 규칙 ② 시장별 일일 상한 ③ 본문 확보
가능 여부(PDF/설명 300자 이상)로 걸러 하루 십수 건만 저장한다(걸러진 공시는 DB에 아예 들어오지 않는다).

시장(GitHub RSSHub 라우트에서 찾은 공식 공개 API — memory newsfinal_exchange_disclosure_apis.md):
  - 중국: 巨潮资讯(cninfo) 통합 공시(상하이·선전) — 제목+PDF. 등급 규칙으로 걸러 PDF 앞부분 텍스트를 뽑는다.
  - 대만: TWSE 중요 공시(OpenAPI) — 이미 '중요 공시'만 제공, 설명(說明) 본문 포함.
저장된 항목은 gemini_writer.OFFICIAL_SOLO_SOURCE_NAMES에 이 소스명이 들어 있어 교차 보도 없이 단독 기사화된다.

실행: python scripts/disclosure_collector.py            # 저장
      python scripts/disclosure_collector.py --dry-run  # 후보만 출력(저장 안 함)
"""
import datetime as dt
import io
import os
import re
import sys

import requests
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(__file__))
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

UA = {"User-Agent": "Mozilla/5.0 (compatible; NewsFinalBot/1.0)"}
CN_SOURCE = "China - 巨潮资讯 중요공시"
TW_SOURCE = "Taiwan - TWSE 중요공시"
DAILY_CAP = {CN_SOURCE: 12, TW_SOURCE: 6}   # 시장별 하루 저장 상한
MIN_TEXT = 300                              # 이보다 짧은 본문은 기사 재료가 못 되므로 저장하지 않는다
MAX_PDF_PAGES = 6

# ── 중국 공시 중요도 등급(작을수록 중요). 제목 기준, 위에서부터 첫 일치 등급. 등급 없으면 저장하지 않는다.
CN_TIERS = [
    (1, re.compile(r"立案|行政处罚|被调查|财务造假|风险警示|终止上市|退市|暂停上市|破产|重整|失联|无法表示意见|控制权|实际控制人")),
    (2, re.compile(r"重大资产重组|重大资产购买|重大资产出售|吸收合并|要约收购|借壳|监管措施|警示函|违规")),
    (3, re.compile(r"业绩预告|业绩快报|盈利预警|业绩预亏|业绩预增|停牌|复牌|重大合同|重大诉讼|收购")),
]
# 절차성·부속 서류는 제목에 위 단어가 들어 있어도 제외(예: 'OO 관련 법률의견서', 진행 상황 '제시성 공고')
CN_NOISE = re.compile(
    r"法律意见书|核查意见|独立董事|董事会会议|监事会|股东大会|会议材料|章程|工作细则|募集资金|现金管理|担保|授信|保荐|承销|摘要|"
    r"更名|日常关联交易|补充公告|提示性公告|进展公告|更正|回购|减持|增持|权益分派|分红|可转换|可转债|向特定对象发行|首次公开发行|"
    r"问询函回复|反馈意见|尽职调查|评估报告|审计报告|内部控制|社会责任|ESG|可持续发展|"
    r"质押|最近五年|整改|自查|说明的公告|专项说明|关于公司及子公司使用|部分股份过户"
)
# 대만 TWSE 중요 공시 중 사소한 것(액면·사명 변경, 주총·배당·자사주·사채·재무보고 절차)은 제외
TW_NOISE = re.compile(r"面額|更名|名稱變更|股東會|股東常會|除權|除息|配息|股利|董事會決議|庫藏股|公司債|私募|財務報告|補充公告|更正|申報")


def now_kst():
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=9)))


def _db():
    from db import insert_raw_candidate  # noqa: WPS433  (SUPABASE 환경변수 필요)
    return insert_raw_candidate


def today_count(source: str) -> int:
    """오늘(KST) 이미 저장한 건수 — 일일 상한 판단용(HEAD 카운트 1회)."""
    url = os.environ["SUPABASE_URL"].rstrip("/") + "/rest/v1/raw_candidates"
    key = os.environ["SUPABASE_SERVICE_KEY"]
    r = requests.get(url, timeout=30, headers={"apikey": key, "Authorization": "Bearer " + key,
                                               "Prefer": "count=exact", "Range": "0-0"},
                     params={"select": "id", "source": f"eq.{source}", "created_at": f"gte.{now_kst().strftime('%Y-%m-%d')}"})
    try:
        return int(r.headers.get("content-range", "*/0").split("/")[-1])
    except ValueError:
        return 0


def today_urls(source: str) -> set:
    """오늘 이미 저장한 공시 주소 — 정기 실행마다 같은 PDF를 다시 받고 상한을 중복 소진하지 않게 건너뛰는 용도."""
    url = os.environ["SUPABASE_URL"].rstrip("/") + "/rest/v1/raw_candidates"
    key = os.environ["SUPABASE_SERVICE_KEY"]
    r = requests.get(url, timeout=30, headers={"apikey": key, "Authorization": "Bearer " + key},
                     params={"select": "url", "source": f"eq.{source}", "created_at": f"gte.{now_kst().strftime('%Y-%m-%d')}",
                             "limit": "100"})
    return {x["url"] for x in r.json()} if r.status_code in (200, 206) else set()


def pdf_text(url: str) -> str:
    """PDF 앞부분 텍스트(최대 MAX_PDF_PAGES쪽). 실패하면 빈 문자열."""
    try:
        from pypdf import PdfReader
        r = requests.get(url, headers=UA, timeout=40)
        r.raise_for_status()
        rd = PdfReader(io.BytesIO(r.content))
        txt = "\n".join((p.extract_text() or "") for p in rd.pages[:MAX_PDF_PAGES])
        return re.sub(r"[ \t]+", " ", re.sub(r"\n{3,}", "\n\n", txt)).strip()[:6000]
    except Exception as e:
        print(f"    ⚠️ PDF 추출 실패: {str(e)[:60]}")
        return ""


def cn_candidates() -> list:
    """cninfo 오늘 공시 전량 조회(상하이·선전, 30건 단위 페이지) → 등급 규칙 통과분만 등급순 반환. 저장 전이라 DB 부하 없음."""
    day = now_kst().strftime("%Y-%m-%d")
    ref = {"Referer": "http://www.cninfo.com.cn/new/commonUrl/pageOfSearch?url=disclosure/list/search"}
    rows = []
    for column, plate in (("szse", "sz"), ("sse", "sh")):
        for page in range(1, 60):
            r = requests.post("http://www.cninfo.com.cn/new/hisAnnouncement/query", timeout=30, headers={**UA, **ref}, data={
                "stock": "", "tabName": "fulltext", "pageSize": "30", "pageNum": str(page), "column": column,
                "category": "", "plate": plate, "seDate": f"{day}~{day}", "searchkey": "", "secid": "", "sortName": "",
                "sortType": "", "isHLtitle": "true"})
            part = (r.json().get("announcements") or [])
            rows += part
            if len(part) < 30:
                break
    print(f"  [cninfo] 오늘 공시 {len(rows)}건 조회")
    out = []
    for x in rows:
        title = re.sub(r"<[^>]+>", "", x.get("announcementTitle") or "")
        if CN_NOISE.search(title):
            continue
        tier = next((t for t, rx in CN_TIERS if rx.search(title)), None)
        if tier is None:
            continue
        out.append((tier, x, title))
    out.sort(key=lambda t: (t[0], -(t[1].get("announcementTime") or 0)))
    print(f"  [cninfo] 중요도 규칙 통과 {len(out)}건 ({len(out) / max(1, len(rows)):.1%})")
    return out


def tw_candidates() -> list:
    r = requests.get("https://openapi.twse.com.tw/v1/opendata/t187ap04_L", headers=UA, timeout=30)
    r.raise_for_status()
    out = []
    for x in r.json():
        subject = (x.get("主旨 ") or x.get("主旨") or "").strip()
        body = (x.get("說明") or "").strip()
        text = f"{subject}\n{body}".strip()
        if len(text) >= MIN_TEXT and not TW_NOISE.search(subject):
            out.append((x, subject, text))
    print(f"  [TWSE] 중요 공시 {len(r.json())}건 중 본문 {MIN_TEXT}자 이상 {len(out)}건")
    return out


def run(dry: bool):
    print(f"\n[disclosure_collector] 시작 {now_kst().strftime('%Y-%m-%d %H:%M')} KST{' (dry-run)' if dry else ''}")
    insert = None if dry else _db()
    saved = 0

    # ── 중국
    room = DAILY_CAP[CN_SOURCE] - (0 if dry else today_count(CN_SOURCE))
    have = set() if dry else today_urls(CN_SOURCE)
    try:
        for tier, x, title in cn_candidates():
            if room <= 0:
                break
            link = (f"http://www.cninfo.com.cn/new/disclosure/detail?stockCode={x.get('secCode')}&announcementId="
                    f"{x.get('announcementId')}&orgId={x.get('orgId')}&announcementTime={x.get('announcementTime')}")
            if link in have:
                continue
            adj = x.get("adjunctUrl") or ""
            text = pdf_text("http://static.cninfo.com.cn/" + adj) if adj.lower().endswith(".pdf") else ""
            if len(text) < MIN_TEXT:
                continue
            name = f"{x.get('secName')}({x.get('secCode')})"
            print(f"  ✓ [중국 등급{tier}] {name} {title[:40]} (본문 {len(text)}자)")
            if not dry:
                pub = dt.datetime.fromtimestamp((x.get("announcementTime") or 0) / 1000, dt.timezone.utc).isoformat()
                insert(f"{name} {title}", "", text[:600], "", link, CN_SOURCE, "경제", "금융/증권", "asia", "중국",
                       score=0, full_text=text, source_published_at=pub)
                saved += 1
            room -= 1
    except Exception as e:
        print(f"  ⚠️ 중국 수집 실패(건너뜀): {str(e)[:100]}")

    # ── 대만
    room = DAILY_CAP[TW_SOURCE] - (0 if dry else today_count(TW_SOURCE))
    have = set() if dry else today_urls(TW_SOURCE)
    try:
        for x, subject, text in tw_candidates():
            if room <= 0:
                break
            name = f"{x.get('公司名稱')}({x.get('公司代號')})"
            link = f"https://mops.twse.com.tw/mops/web/t05sr01_1#{x.get('公司代號')}-{x.get('發言日期')}-{x.get('發言時間')}"
            if link in have:
                continue
            print(f"  ✓ [대만] {name} {subject[:40]} (본문 {len(text)}자)")
            if not dry:
                insert(f"{name} {subject}", "", text[:600], "", link, TW_SOURCE, "경제", "금융/증권", "asia", "대만",
                       score=0, full_text=text)
                saved += 1
            room -= 1
    except Exception as e:
        print(f"  ⚠️ 대만 수집 실패(건너뜀): {str(e)[:100]}")
    print(f"[disclosure_collector] 완료 — {'후보 출력만' if dry else f'{saved}건 저장'}")


if __name__ == "__main__":
    run("--dry-run" in sys.argv)
