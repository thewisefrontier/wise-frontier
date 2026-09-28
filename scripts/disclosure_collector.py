# -*- coding: utf-8 -*-
"""
scripts/disclosure_collector.py
-------------------------------
거래소 공시 중 '중요 공시'만 골라 raw_candidates에 넣는 수집기 (2026-09-28).

사용자 지시: "믿을 수 있는 소스(거래소 공지·공시)는 그냥 기사로 낸다" + "주요 공시만 필터링해서 필요한 것만 가져와야지. 안 그러면 DB 터진다"
+ "다 넣어. 대신 필터링 빡빡하게 해서 DB 지키고". → 공시 전량을 저장하지 않는다. 수집 단계에서
  ① 시장별 중요도 등급 규칙(등급 없으면 버림) ② 정형·절차성 공시 제외 ③ 시장별 하루 상한 ④ 본문 300자 이상 확보 가능한 것만
저장한다(걸러진 공시는 DB에 아예 들어오지 않는다). 시장별 하루 상한 합계 ≤ 44건.

시장(GitHub RSSHub 라우트 등에서 찾은 공식 공개 API — memory newsfinal_exchange_disclosure_apis.md):
  중국 cninfo(상하이·선전) · 대만 TWSE 중요공시 · 일본 TDnet(Yanoshin API) · 홍콩 HKEXnews · 미국 SEC EDGAR 8-K
  (호주 ASX는 공시 PDF를 받으려면 사이트 약관 동의 양식 제출이 필요해 제외, 한국 DART는 OpenDART 키가 필요해 제외)
저장된 항목은 gemini_writer.OFFICIAL_SOLO_SOURCE_NAMES에 이 소스명이 들어 있어 교차 보도 없이 단독 기사화된다.

실행: python scripts/disclosure_collector.py            # 저장
      python scripts/disclosure_collector.py --dry-run  # 후보만 출력(저장 안 함)
"""
import datetime as dt
import html as htmllib
import io
import os
import re
import sys

import requests
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(__file__))
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

UA = {"User-Agent": "Mozilla/5.0 (compatible; NewsFinalBot/1.0)"}
SEC_UA = {"User-Agent": "NewsFinal admin@newsfinal.co.kr"}  # SEC는 연락처가 든 UA를 요구한다
CN_SOURCE = "China - 巨潮资讯 중요공시"
TW_SOURCE = "Taiwan - TWSE 중요공시"
JP_SOURCE = "Japan - TDnet 중요공시"
HK_SOURCE = "Hong Kong - HKEXnews 중요공시"
US_SOURCE = "US - SEC EDGAR 8-K 중요공시"
KR_SOURCE = "Korea - DART 중요공시"
ALL_SOURCES = (CN_SOURCE, TW_SOURCE, JP_SOURCE, HK_SOURCE, US_SOURCE, KR_SOURCE)
DAILY_CAP = {CN_SOURCE: 12, TW_SOURCE: 6, JP_SOURCE: 8, HK_SOURCE: 8, US_SOURCE: 10, KR_SOURCE: 6}  # 시장별 하루 저장 상한(합계 50)
MIN_TEXT = 300      # 이보다 짧은 본문은 기사 재료가 못 되므로 저장하지 않는다
MAX_PDF_PAGES = 6

# ── 중국(cninfo): 제목 기준 등급(작을수록 중요). 등급 없으면 저장하지 않는다.
CN_TIERS = [
    (1, re.compile(r"立案|行政处罚|被调查|财务造假|风险警示|终止上市|退市|暂停上市|破产|重整|失联|无法表示意见|控制权|实际控制人")),
    (2, re.compile(r"重大资产重组|重大资产购买|重大资产出售|吸收合并|要约收购|借壳|监管措施|警示函|违规")),
    (3, re.compile(r"业绩预告|业绩快报|盈利预警|业绩预亏|业绩预增|停牌|复牌|重大合同|重大诉讼|收购")),
]
CN_NOISE = re.compile(
    r"法律意见书|核查意见|独立董事|董事会会议|监事会|股东大会|会议材料|章程|工作细则|募集资金|现金管理|担保|授信|保荐|承销|摘要|"
    r"更名|日常关联交易|补充公告|提示性公告|进展公告|更正|回购|减持|增持|权益分派|分红|可转换|可转债|向特定对象发行|首次公开发行|"
    r"问询函回复|反馈意见|尽职调查|评估报告|审计报告|内部控制|社会责任|ESG|可持续发展|"
    r"质押|最近五年|整改|自查|说明的公告|专项说明|关于公司及子公司使用|部分股份过户"
)
# ── 대만 TWSE: 이미 '중요 공시'만 제공되므로 사소한 것만 제외
TW_NOISE = re.compile(r"面額|更名|名稱變更|股東會|股東常會|除權|除息|配息|股利|董事會決議|庫藏股|公司債|私募|財務報告|補充公告|更正|申報")
# ── 일본 TDnet
JP_TIERS = [
    (1, re.compile(r"上場廃止|監理銘柄|整理銘柄|特設注意市場|不適切な会計|不正|破産|民事再生|会社更生|債務超過|特別調査委員会|第三者委員会|上場維持")),
    (2, re.compile(r"公開買付|ＴＯＢ|TOB|合併|株式交換|株式移転|事業譲渡|会社分割|経営統合|買収|完全子会社化|第三者割当|資本業務提携|ＭＢＯ|MBO|支配株主の異動|親会社の異動")),
    (3, re.compile(r"業績予想の修正|特別損失|特別利益|減損損失|大型受注|新株予約権付社債|公募増資")),
]
JP_NOISE = re.compile(r"自己株式|自己株券|決算短信|定時株主総会|招集|役員人事|人事異動|配当|株主優待|投資法人|投資証券|ＥＴＦ|ETF|ＥＴＮ|基準価額|"
                      r"譲渡制限付株式|株式報酬|ストックオプション|コーポレート・ガバナンス|訂正|リリース|シンジケートローン|資金の借入")
# ── 홍콩 HKEXnews(lTxt 분류 + 제목, 번체 중국어)
HK_TIERS = [
    (1, re.compile(r"除牌|取消上市|清盤|破產|呈請|暫停買賣|停牌|核數師辭任|延遲刊發|欺詐|調查")),
    (2, re.compile(r"收購守則|全面要約|私有化|非常重大|主要交易|反收購|合併")),
    (3, re.compile(r"盈利警告|盈喜|內幕消息|須予披露")),
]
HK_NOISE = re.compile(r"翌日披露|通函|委任代表|中期報告|年報|年度報告|董事會會議|職權範圍|董事名單|股息|購回|月報表|海外監管|環境、社會|投票表決|章程")
# ── 미국 EDGAR 8-K: 항목 번호 기준
US_TIERS = [
    (1, re.compile(r"Item (1\.03|3\.01|4\.02|1\.05|5\.01)\b")),  # 파산·상장폐지 통지·재무제표 신뢰 철회·사이버 사고·지배권 변경
    (2, re.compile(r"Item (2\.01|4\.01|2\.06|2\.04)\b")),        # 인수·처분 완료·감사인 변경·손상차손·채무 가속
]


# ── 한국 DART(OpenDART list API, 키는 머니파이널과 같은 DART_API_KEY): 보고서명 기준. 코스피·코스닥 상장사만.
KR_TIERS = [
    (1, re.compile(r"상장폐지결정|상장폐지사유|감사의견.*(거절|부적정)|횡령|배임|회생절차개시|파산신청|부도발생|영업정지|불성실공시법인지정|관리종목지정")),
    (2, re.compile(r"합병결정|분할결정|주식교환|공개매수|최대주주.*변경|경영권|주식양수도")),
    (3, re.compile(r"영업\(잠정\)실적|매출액또는손익구조|단일판매|공급계약체결|조회공시|유상증자결정|타법인주식및출자증권취득결정")),
]
KR_NOISE = re.compile(r"정정|기재정정|첨부|임원|주요주주|대량보유|자기주식|사업보고서|분기보고서|반기보고서|감사보고서|주주총회|의결권|증권발행실적|"
                      r"투자설명서|일괄신고|배당|주식매수선택권|풍문또는보도|현금ㆍ현물배당|"
                      r"투자유의안내|기타시장안내|투자주의|투자경고|매매거래정지|거래정지|해제|우려|예고|스팩|예비심사|시장조치")


def now_kst():
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=9)))


def _db():
    from db import insert_raw_candidate  # SUPABASE 환경변수 필요
    return insert_raw_candidate


def _rest():
    return os.environ["SUPABASE_URL"].rstrip("/") + "/rest/v1/raw_candidates", {
        "apikey": os.environ["SUPABASE_SERVICE_KEY"], "Authorization": "Bearer " + os.environ["SUPABASE_SERVICE_KEY"]}


def today_count(source: str) -> int:
    """오늘(KST) 이미 저장한 건수 — 일일 상한 판단용(HEAD 카운트 1회)."""
    url, h = _rest()
    r = requests.get(url, timeout=30, headers={**h, "Prefer": "count=exact", "Range": "0-0"},
                     params={"select": "id", "source": f"eq.{source}", "created_at": f"gte.{now_kst().strftime('%Y-%m-%d')}"})
    try:
        return int(r.headers.get("content-range", "*/0").split("/")[-1])
    except ValueError:
        return 0


def today_urls(source: str) -> set:
    """오늘 이미 저장한 공시 주소 — 정기 실행마다 같은 문서를 다시 받고 상한을 중복 소진하지 않게 건너뛰는 용도."""
    url, h = _rest()
    r = requests.get(url, timeout=30, headers=h,
                     params={"select": "url", "source": f"eq.{source}", "created_at": f"gte.{now_kst().strftime('%Y-%m-%d')}", "limit": "100"})
    return {x["url"] for x in r.json()} if r.status_code in (200, 206) else set()


def pdf_text(url: str, headers=None) -> str:
    """PDF 앞부분 텍스트(최대 MAX_PDF_PAGES쪽). 실패하면 빈 문자열."""
    try:
        from pypdf import PdfReader
        r = requests.get(url, headers=headers or UA, timeout=40)
        r.raise_for_status()
        rd = PdfReader(io.BytesIO(r.content))
        txt = "\n".join((p.extract_text() or "") for p in rd.pages[:MAX_PDF_PAGES])
        return re.sub(r"[ \t]+", " ", re.sub(r"\n{3,}", "\n\n", txt)).strip()[:6000]
    except Exception as e:
        print(f"    ⚠️ PDF 추출 실패: {str(e)[:60]}")
        return ""


def html_text(url: str, headers=None) -> str:
    try:
        r = requests.get(url, headers=headers or UA, timeout=40)
        r.raise_for_status()
        t = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", " ", r.text)
        t = htmllib.unescape(re.sub(r"(?s)<[^>]+>", " ", t))
        return re.sub(r"\s+", " ", t).strip()[:6000]
    except Exception as e:
        print(f"    ⚠️ HTML 추출 실패: {str(e)[:60]}")
        return ""


# ── 시장별 후보 생성: dict(tier, name, title, link, get_text(), pub, country)를 등급순으로 반환 ──────────────
def cn_candidates() -> list:
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
    out = []
    for x in rows:
        title = re.sub(r"<[^>]+>", "", x.get("announcementTitle") or "")
        if CN_NOISE.search(title):
            continue
        tier = next((t for t, rx in CN_TIERS if rx.search(title)), None)
        adj = x.get("adjunctUrl") or ""
        if tier is None or not adj.lower().endswith(".pdf"):
            continue
        out.append({"tier": tier, "name": f"{x.get('secName')}({x.get('secCode')})", "title": title, "country": "중국",
                    "link": (f"http://www.cninfo.com.cn/new/disclosure/detail?stockCode={x.get('secCode')}&announcementId="
                             f"{x.get('announcementId')}&orgId={x.get('orgId')}&announcementTime={x.get('announcementTime')}"),
                    "get_text": (lambda a=adj: pdf_text("http://static.cninfo.com.cn/" + a)),
                    "pub": dt.datetime.fromtimestamp((x.get("announcementTime") or 0) / 1000, dt.timezone.utc).isoformat(),
                    "sort": -(x.get("announcementTime") or 0)})
    print(f"  [중국] 오늘 공시 {len(rows)}건 → 규칙 통과 {len(out)}건 ({len(out) / max(1, len(rows)):.1%})")
    return out


def tw_candidates() -> list:
    r = requests.get("https://openapi.twse.com.tw/v1/opendata/t187ap04_L", headers=UA, timeout=30)
    r.raise_for_status()
    data = r.json()
    out = []
    for x in data:
        subject = (x.get("主旨 ") or x.get("主旨") or "").strip()
        text = f"{subject}\n{(x.get('說明') or '').strip()}".strip()
        if len(text) >= MIN_TEXT and not TW_NOISE.search(subject):
            out.append({"tier": 2, "name": f"{x.get('公司名稱')}({x.get('公司代號')})", "title": subject, "country": "대만",
                        "link": f"https://mops.twse.com.tw/mops/web/t05sr01_1#{x.get('公司代號')}-{x.get('發言日期')}-{x.get('發言時間')}",
                        "get_text": (lambda t=text: t), "pub": None, "sort": 0})
    print(f"  [대만] 중요 공시 {len(data)}건 → 규칙 통과 {len(out)}건")
    return out


def jp_candidates() -> list:
    r = requests.get("https://webapi.yanoshin.jp/webapi/tdnet/list/recent.json?limit=1000", headers=UA, timeout=40)
    r.raise_for_status()
    items = [x["Tdnet"] for x in r.json().get("items", [])]
    today = now_kst().strftime("%Y-%m-%d")
    out = []
    for t in items:
        title = t.get("title") or ""
        if not (t.get("pubdate") or "").startswith(today) or JP_NOISE.search(title):
            continue
        tier = next((k for k, rx in JP_TIERS if rx.search(title)), None)
        if tier is None or not t.get("document_url"):
            continue
        out.append({"tier": tier, "name": f"{t.get('company_name')}({str(t.get('company_code'))[:4]})", "title": title, "country": "일본",
                    "link": t["document_url"], "get_text": (lambda u=t["document_url"]: pdf_text(u)),
                    "pub": None, "sort": -int(re.sub(r"\D", "", t.get("pubdate") or "0") or 0)})
    print(f"  [일본] TDnet 최근 {len(items)}건 중 오늘분 → 규칙 통과 {len(out)}건")
    return out


def hk_candidates() -> list:
    r = requests.get("https://www1.hkexnews.hk/ncms/json/eds/lcisehk1relsdc_1.json", headers=UA, timeout=40)
    r.raise_for_status()
    rows = r.json().get("newsInfoLst", [])
    out = []
    for x in rows:
        label = f"{x.get('lTxt') or ''} {x.get('title') or ''}"
        if HK_NOISE.search(label) or (x.get("ext") or "").lower() != "pdf":
            continue
        tier = next((k for k, rx in HK_TIERS if rx.search(label)), None)
        if tier is None:
            continue
        st = (x.get("stock") or [{}])[0]
        out.append({"tier": tier, "name": f"{st.get('sn')}({st.get('sc')})", "title": (x.get("title") or "").strip(), "country": "홍콩",
                    "link": "https://www1.hkexnews.hk" + x["webPath"],
                    "get_text": (lambda p=x["webPath"]: pdf_text("https://www1.hkexnews.hk" + p)),
                    "pub": None, "sort": -int(x.get("newsId") or 0)})
    print(f"  [홍콩] HKEX 최신 {len(rows)}건 → 규칙 통과 {len(out)}건")
    return out


def us_candidates() -> list:
    r = requests.get("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&count=100&output=atom", headers=SEC_UA, timeout=40)
    r.raise_for_status()
    ents = re.findall(r"<entry>.*?</entry>", r.text, re.S)
    out = []
    for e in ents:
        plain = htmllib.unescape(re.sub(r"<[^>]+>", " ", htmllib.unescape(e)))
        tier = next((k for k, rx in US_TIERS if rx.search(plain)), None)
        if tier is None:
            continue
        m = re.search(r"8-K - (.+?) \(\d+\)", plain)
        link = re.search(r'<link[^>]*href="([^"]+)"', e)
        items = ", ".join(sorted(set(re.findall(r"Item \d\.\d\d[^<\n]{0,70}", plain)))[:3])
        if not (m and link):
            continue
        out.append({"tier": tier, "name": m.group(1).strip(), "title": items, "country": "미국", "link": link.group(1),
                    "get_text": (lambda u=link.group(1): _edgar_main_text(u)), "pub": None, "sort": 0})
    print(f"  [미국] EDGAR 8-K 최근 {len(ents)}건 → 규칙 통과 {len(out)}건")
    return out


def _edgar_main_text(index_url: str) -> str:
    """8-K 인덱스 페이지에서 본문(첫 .htm)을 찾아 텍스트를 뽑는다."""
    try:
        idx = requests.get(index_url, headers=SEC_UA, timeout=30).text
        hrefs = [h for h in re.findall(r'href="(/Archives/edgar/data/[^"]+\.htm)"', idx) if "-index" not in h]
        return html_text("https://www.sec.gov" + hrefs[0], SEC_UA) if hrefs else ""
    except Exception as e:
        print(f"    ⚠️ EDGAR 본문 실패: {str(e)[:60]}")
        return ""


def kr_candidates() -> list:
    key = os.environ.get("DART_API_KEY", "")
    if not key:
        print("  [한국] DART_API_KEY 없음 — 건너뜀")
        return []
    day = now_kst().strftime("%Y%m%d")
    rows = []
    for page in range(1, 8):
        r = requests.get("https://opendart.fss.or.kr/api/list.json", timeout=30, params={
            "crtfc_key": key, "bgn_de": day, "end_de": day, "page_no": page, "page_count": 100})
        j = r.json()
        if j.get("status") not in ("000", "013"):
            print(f"  ⚠️ [한국] DART 응답 {j.get('status')} {str(j.get('message'))[:40]}")
            break
        part = j.get("list") or []
        rows += part
        if len(part) < 100:
            break
    out = []
    for x in rows:
        name = x.get("report_nm") or ""
        if x.get("corp_cls") not in ("Y", "K") or KR_NOISE.search(name):
            continue
        tier = next((k for k, rx in KR_TIERS if rx.search(name)), None)
        if tier is None:
            continue
        out.append({"tier": tier, "name": f"{x.get('corp_name')}({x.get('stock_code')})", "title": name.strip(), "country": "한국",
                    "link": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={x.get('rcept_no')}",
                    "get_text": (lambda n=x.get("rcept_no"), k=key: _dart_text(n, k)),
                    "pub": None, "sort": (0 if x.get("corp_cls") == "Y" else 1, -int(x.get("rcept_no") or 0))})
    print(f"  [한국] DART 오늘 공시 {len(rows)}건 → 규칙 통과 {len(out)}건")
    return out


def _dart_text(rcept_no: str, key: str) -> str:
    """OpenDART 원문(document.xml, ZIP 안의 XML)에서 텍스트를 뽑는다."""
    try:
        import zipfile
        r = requests.get("https://opendart.fss.or.kr/api/document.xml", params={"crtfc_key": key, "rcept_no": rcept_no}, timeout=40)
        z = zipfile.ZipFile(io.BytesIO(r.content))
        raw = z.read(z.namelist()[0]).decode("utf-8", "ignore")
        t = htmllib.unescape(re.sub(r"(?s)<[^>]+>", " ", raw))
        return re.sub(r"\s+", " ", t).strip()[:6000]
    except Exception as e:
        print(f"    ⚠️ DART 원문 실패: {str(e)[:60]}")
        return ""


MARKETS = [(CN_SOURCE, cn_candidates), (TW_SOURCE, tw_candidates), (JP_SOURCE, jp_candidates),
           (HK_SOURCE, hk_candidates), (US_SOURCE, us_candidates), (KR_SOURCE, kr_candidates)]


def run(dry: bool, only=None):
    print(f"\n[disclosure_collector] 시작 {now_kst().strftime('%Y-%m-%d %H:%M')} KST{' (dry-run)' if dry else ''}")
    insert = None if dry else _db()
    saved = 0
    for source, fn in MARKETS:
        if only and only not in source:
            continue
        try:
            room = DAILY_CAP[source] - (0 if dry else today_count(source))
            if room <= 0:
                print(f"  [{source}] 오늘 상한 도달 — 건너뜀")
                continue
            have = set() if dry else today_urls(source)
            cands = sorted(fn(), key=lambda c: (c["tier"], c.get("sort", 0)))
            for c in cands:
                if room <= 0:
                    break
                if c["link"] in have:
                    continue
                text = c["get_text"]()
                if len(text) < MIN_TEXT:
                    continue
                print(f"    ✓ 등급{c['tier']} {c['name']} {c['title'][:44]} (본문 {len(text)}자)")
                if not dry:
                    insert(f"{c['name']} {c['title']}".strip(), "", text[:600], "", c["link"], source, "경제", "금융/증권", "asia" if c["country"] != "미국" else "global",
                           c["country"], score=0, full_text=text, source_published_at=c.get("pub"))
                    saved += 1
                room -= 1
        except Exception as e:
            print(f"  ⚠️ [{source}] 수집 실패(건너뜀): {str(e)[:100]}")
    print(f"[disclosure_collector] 완료 — {'후보 출력만' if dry else f'{saved}건 저장'}")


if __name__ == "__main__":
    only = sys.argv[sys.argv.index("--only") + 1] if "--only" in sys.argv else None
    run("--dry-run" in sys.argv, only)
