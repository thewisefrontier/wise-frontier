# -*- coding: utf-8 -*-
"""오늘(또는 --date) 미발행 종합기사 재검증 → 문제 없는 것만 발행. 2026-09-28 사용자 지시("오늘 미발행된 기사 중에 검증해보고
문제 없는 건 내보내도록").

대상: 보류 사유가 오직 "[위키 미확인]"뿐인 기사(위키 조회가 실존 기관·기업을 못 찾는 오탐이 대부분 — fabrication_guard 2026-09-28 참고).
  Gemini가 원문 대조로 지적한 "[이름]/[수식어]", 번역 누락, 복수 토픽, 중복, 날짜환각, 분량 부족은 여기서 건드리지 않는다.
검증: 보류된 행엔 원문이 남아 있지 않다. 수집 원본(raw_candidates)은 국가가 비어 있는 경우가 82%라 국가로는 못 찾으므로,
  기사 속 원어 병기 이름 + NVIDIA가 뽑은 원어 키워드로 원본의 제목·요약·본문을 검색해(기사 생성 96시간 전부터) 숫자·이름이 겹치는
  원문을 근거로 삼는다. ① 위키 미확인 이름이 그 원문에 근거가 있는지 ② 기사 전체에 원문 근거 없는 문장이 없는지(둘 다 NVIDIA).
  둘 다 통과한 것만 후보. 원문을 못 찾으면 보류 유지(fail-closed).
  2026-09-29~: 생성 때 저장한 원본 id(source_data.src_ids)가 있으면 키워드 검색 없이 그 원문을 쓴다(비라틴 원문도 찾음).

실행: python scripts/reverify_held.py                     # 검증만(발행 안 함) → %TEMP%/reverify_result.json, reverify_result_ids.json
      python scripts/reverify_held.py --apply ids.json    # 저장된 통과 id만 발행(이미지 검색 + Aiven 미러, 텔레그램 발송은 안 함)
"""
import datetime as dt
import json
import os
import re
import sys
import time

import requests
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(__file__))
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

URL = os.environ["SUPABASE_URL"].rstrip("/")
KEY = os.environ["SUPABASE_SERVICE_KEY"]
H = {"apikey": KEY, "Authorization": "Bearer " + KEY, "Content-Type": "application/json"}
OTHER_REASONS = ("번역 누락", "복수 토픽", "중복", "날짜환각", "분량 부족", "[이름]", "[수식어]")


def held_articles(date):
    r = requests.get(URL + "/rest/v1/articles", headers={**H, "Range": "0-499"}, timeout=60, params={
        "select": "id,title_ko,summary_ko,country,category,update_log,created_at,source_data", "source": "eq.NewsFinal",
        "is_published": "eq.false", "subcategory": "like.cluster_*", "created_at": "gte." + date, "order": "created_at.asc"})
    r.raise_for_status()
    out = []
    for x in r.json():
        notes = " ".join((n.get("note") or "") for n in (x["update_log"] or []))
        if "[위키 미확인]" not in notes or any(k in notes for k in OTHER_REASONS):
            continue
        names = [s.strip() for s in notes.split("[위키 미확인]", 1)[1].split("\n")[0].split(",") if s.strip()]
        out.append({**x, "names": names})
    return out


def search_keywords(article):
    """원문 검색용 원어 키워드: 본문의 괄호 병기 원어 + NVIDIA가 뽑은 핵심 고유명사(원어 표기)."""
    from nvidia_client import call_nvidia
    body = article["summary_ko"] or ""
    kws = []
    for m in re.findall(r"\(([A-Za-z][^)]{4,50})\)", body):
        if m.strip() not in kws:
            kws.append(m.strip())
    try:
        resp = call_nvidia("다음 한국어 기사가 다룬 사건을 원문(영어·프랑스어 등)에서 찾는 데 쓸 핵심 고유명사 3개를 "
                           "원어(로마자) 표기로 쉼표로만 나열하세요. 설명 금지.\n\n" + body[:1500], max_tokens=80) or ""
        for w in resp.split(","):
            w = w.strip().strip("\"'")
            if 4 <= len(w) <= 50 and re.search(r"[A-Za-z]", w) and w not in kws:
                kws.append(w)
    except Exception:
        pass
    return kws[:5]


def raw_sources(article):
    start = (dt.datetime.strptime(article["created_at"][:16], "%Y-%m-%d %H:%M") - dt.timedelta(hours=96)).strftime("%Y-%m-%d %H:%M")
    rows, seen = [], set()
    for kw in search_keywords(article):
        safe = re.sub(r"[,()*]", " ", kw)
        r = requests.get(URL + "/rest/v1/raw_candidates", headers=H, timeout=90, params={
            "select": "id,title_en,summary_en,full_text,source", "created_at": "gte." + start, "limit": "6",
            "or": f"(title_en.ilike.*{safe}*,summary_en.ilike.*{safe}*,full_text.ilike.*{safe}*)"})
        if r.status_code in (200, 206):
            for x in r.json():
                if x["id"] not in seen:
                    seen.add(x["id"])
                    rows.append(x)
    return rows


def stored_sources(article):
    """기사 생성 때 저장한 원본 id(source_data.src_ids, 2026-09-29~)로 원문을 바로 읽는다. 없거나 보존기간(96시간)이 지나 지워졌으면 []."""
    ids = ((article.get("source_data") or {}).get("src_ids") or [])[:12]
    if not ids:
        return []
    r = requests.get(URL + "/rest/v1/raw_candidates", headers=H, timeout=60, params={
        "select": "id,title_en,summary_en,full_text,source", "id": "in.(" + ",".join(str(int(i)) for i in ids) + ")"})
    return r.json() if r.status_code in (200, 206) else []


def find_facts(article):
    rows = stored_sources(article)
    if rows:
        per = max(500, 6000 // len(rows))
        facts = "\n\n".join(f"[{r['source']}] {r.get('title_en') or ''}\n{(r.get('full_text') or r.get('summary_en') or '')[:per]}"
                            for r in rows)
        return facts[:6000], ["src_ids"]
    body = article["summary_ko"] or ""
    nums = {n.replace(",", "") for n in re.findall(r"\d[\d,.]*\d|\d", body) if len(n.replace(",", "")) >= 2}
    latin = {m.strip().lower() for m in re.findall(r"\(([A-Za-z][^)]{2,40})\)", body)}
    scored = []
    for row in raw_sources(article):
        text = " ".join(filter(None, [row.get("title_en"), row.get("summary_en"), row.get("full_text")]))
        t = text.replace(",", "").lower()
        score = sum(1 for n in nums if n in t) + 2 * sum(1 for w in latin if w in t)
        if score >= 2:
            scored.append((score, row, text))
    scored.sort(key=lambda x: -x[0])
    facts = "\n\n".join(f"[{r['source']}] {r.get('title_en') or ''}\n{t[:2500]}" for _, r, t in scored[:4])
    return facts[:6000], [s for s, _, _ in scored[:4]]


def verify(limit=None, date=None):
    from fabrication_guard import names_without_source_support, unsupported_claims
    date = date or dt.datetime.now().strftime("%Y-%m-%d")
    arts = held_articles(date)[:limit]
    print(f"재검증 대상(사유가 [위키 미확인]뿐): {len(arts)}건", flush=True)
    results = []
    for i, a in enumerate(arts, 1):
        res = {"id": a["id"], "title": a["title_ko"], "names": a["names"], "country": a["country"]}
        facts, scores = find_facts(a)
        if not facts:
            res.update(ok=False, why="원문(수집 원본)을 찾지 못함")
        else:
            left = names_without_source_support(a["names"], facts)
            time.sleep(1.5)
            unsup = unsupported_claims(a["summary_ko"], facts)
            time.sleep(1.5)
            res.update(scores=scores, unsupported_names=left, unsupported_claims=unsup[:300],
                       ok=(not left and not unsup), why="통과" if (not left and not unsup) else "원문 근거 부족")
        print(f"  [{i}/{len(arts)}] {'OK' if res['ok'] else 'HOLD'} {a['id']} {a['title_ko'][:34]} - {res['why']}", flush=True)
        results.append(res)
    return results


def publish(ids):
    import gemini_writer as gw
    from article_store import _mirror_final_article
    done = 0
    for aid in ids:
        r = requests.get(URL + "/rest/v1/articles", headers=H, timeout=60, params={"select": "*", "id": f"eq.{aid}"})
        row = (r.json() or [None])[0]
        if not row or row["is_published"]:
            continue
        img, cred = gw.fetch_article_image(row["title_ko"], row["summary_ko"], "")
        log = list(row.get("update_log") or []) + [{"timestamp": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
                                                    "note": "재검수 통과 후 발행(위키 미확인 오탐 - 원문 근거 확인)"}]
        p = requests.patch(URL + "/rest/v1/articles", headers={**H, "Prefer": "return=representation"}, timeout=60,
                           params={"id": f"eq.{aid}", "is_published": "eq.false"},
                           json={"is_published": True, "image_url": img or "", "image_credit": cred or "", "update_log": log})
        if p.status_code == 200 and p.json():
            _mirror_final_article(p.json()[0])
            done += 1
            print(f"  published id={aid} {row['title_ko'][:40]} (image {'yes' if img else 'no'})", flush=True)
    print(f"발행 완료 {done}건")


if __name__ == "__main__":
    scratch = os.path.join(os.environ.get("TEMP", "."), "reverify_result.json")
    if "--apply" in sys.argv:
        publish(json.load(open(sys.argv[sys.argv.index("--apply") + 1], encoding="utf-8")))
    else:
        lim = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else None
        res = verify(limit=lim)
        json.dump(res, open(scratch, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        ok = [r["id"] for r in res if r["ok"]]
        json.dump(ok, open(scratch.replace(".json", "_ids.json"), "w"))
        print(f"\n통과 {len(ok)} / {len(res)}건 - 결과: {scratch}")
