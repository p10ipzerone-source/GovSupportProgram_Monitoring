"""지원사업 공고 수집기
GitHub Actions에서 주기적으로 실행되어 data.json을 갱신한다.
- RIPC(pms.ripc.org) 지원사업 공고
- 수출바우처(exportvoucher.com) 공지
- 기업마당(bizinfo.go.kr) 키워드 검색 (마감 제외)
각 사이트 원본 HTML은 debug/ 폴더에 저장 (파서 점검용).
"""
import json
import os
import re
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlencode

import requests
from bs4 import BeautifulSoup

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).parent
DATA = ROOT / "data.json"
DEBUG = ROOT / "debug"
DEBUG.mkdir(exist_ok=True)

# ───────── 설정 ─────────
# 기업마당 검색 키워드 (공고명 검색, 마감 공고 제외)
BIZINFO_KEYWORDS = [
    "창업성장기술개발",
    "발굴육성",
    "TIPS",
    "글로벌 기업",
    "데이터바우처",
    "AI 바우처",
]

RIPC_LIST = "https://pms.ripc.org/pms/biz/applicant/notice/list.do"
EXPORT_LIST = "https://www.exportvoucher.com/portal/board/boardList?bbs_id=1"
BIZINFO_LIST = "https://www.bizinfo.go.kr/sii/siia/selectSIIA200View.do"
BIZINFO_API = "https://www.bizinfo.go.kr/uss/rss/bizinfoApi.do"  # 선택: BIZINFO_KEY 시크릿 있을 때

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9",
}

S = requests.Session()
S.headers.update(HEADERS)


# ───────── 공통 유틸 ─────────
def now_kst():
    return datetime.now(KST)


def save_debug(name, text):
    (DEBUG / f"{name}.html").write_text(text, encoding="utf-8")


def get(url, **kw):
    r = S.get(url, timeout=30, **kw)
    r.raise_for_status()
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding
    return r


def clean(s):
    return re.sub(r"\s+", " ", s or "").strip()


DATE_RE = re.compile(r"(20\d{2})[.\-/년 ]\s*(\d{1,2})[.\-/월 ]\s*(\d{1,2})")


def dates_in(text):
    out = []
    for y, m, d in DATE_RE.findall(text or ""):
        try:
            out.append(datetime(int(y), int(m), int(d)).strftime("%Y-%m-%d"))
        except ValueError:
            pass
    return out


HEADER_MAP = [  # (표준키, 헤더에 포함될 단어들)
    ("title", ["제목", "사업명", "공고명"]),
    ("period", ["기간"]),
    ("status", ["상태"]),
    ("type", ["사업형태", "지원분야", "구분", "분류", "유형"]),
    ("center", ["센터", "지역"]),
    ("org", ["소관", "수행기관", "주관", "기관"]),
    ("date", ["등록일", "작성일", "게시일"]),
    ("no", ["번호"]),
    ("views", ["조회"]),
]


def norm_header(h):
    for key, words in HEADER_MAP:
        if any(w in h for w in words):
            return key
    return h


JS_ARGS = re.compile(r"""['"]([^'"]+)['"]|(\d+)""")


def parse_tables(soup, base):
    """페이지의 모든 표를 행 단위 dict로 변환."""
    rows = []
    for table in soup.find_all("table"):
        ths = [clean(th.get_text()) for th in table.select("thead th")] or [
            clean(th.get_text()) for th in table.select("tr th")
        ]
        keys = [norm_header(h) for h in ths]
        body = table.find("tbody") or table
        for tr in body.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 3:
                continue
            item = {}
            for i, td in enumerate(tds):
                k = keys[i] if i < len(keys) else f"col{i}"
                v = clean(td.get_text(" "))
                item[k] = f"{item[k]} / {v}" if item.get(k) and v else (item.get(k) or v)
            a = tr.find("a")
            link, js = None, None
            if a:
                href = a.get("href", "")
                onclick = a.get("onclick", "") or tr.get("onclick", "")
                if href and not href.startswith(("#", "javascript")):
                    link = urljoin(base, href)
                else:
                    src = onclick or href
                    if src:
                        js = src
                if not item.get("title"):
                    item["title"] = clean(a.get_text(" "))
            elif tr.get("onclick"):
                js = tr.get("onclick")
            item["_link"] = link
            item["_js"] = js
            rows.append(item)
    return rows


def deadline_of(period_text):
    ds = dates_in(period_text)
    return ds[-1] if ds else None


def start_of(period_text):
    ds = dates_in(period_text)
    return ds[0] if len(ds) >= 2 else None


# ───────── 1. RIPC ─────────
def scrape_ripc():
    out = {"items": [], "meta": {}}
    r = get(RIPC_LIST)
    save_debug("ripc_list", r.text)
    soup = BeautifulSoup(r.text, "html.parser")

    # 필터 옵션(센터, 사업형태) 수집
    for sel in soup.find_all("select"):
        label = ""
        if sel.get("id"):
            lab = soup.find("label", attrs={"for": sel["id"]})
            label = clean(lab.get_text()) if lab else ""
        name = sel.get("name") or sel.get("id") or ""
        opts = [
            {"value": o.get("value", ""), "text": clean(o.get_text())}
            for o in sel.find_all("option")
        ]
        out["meta"][name or label] = {"label": label, "options": opts}

    rows = parse_tables(soup, RIPC_LIST)

    # 목록이 AJAX로 그려지는 경우 대비: 페이지 크기 키워서 재요청 시도
    if not [x for x in rows if x.get("title")]:
        for params in (
            {"pageUnit": 50, "pageIndex": 1},
            {"recordCountPerPage": 50, "pageIndex": 1},
        ):
            try:
                r2 = S.post(RIPC_LIST, data=params, timeout=30)
                r2.encoding = r2.apparent_encoding
                save_debug("ripc_list_post", r2.text)
                rows = parse_tables(BeautifulSoup(r2.text, "html.parser"), RIPC_LIST)
                if [x for x in rows if x.get("title")]:
                    break
            except Exception:
                pass

    # JS 안의 .do URL 후보 기록 (AJAX 엔드포인트 탐색용)
    out["meta"]["_ajax_candidates"] = sorted(
        set(re.findall(r"['\"](/pms/[^'\"]+?\.do)['\"]", r.text))
    )[:40]

    for x in rows:
        if not x.get("title"):
            continue
        period = x.get("period", "")
        out["items"].append(
            {
                "id": x.get("no") or x["title"],
                "title": x["title"],
                "type": x.get("type", ""),
                "center": x.get("center", ""),
                "status": x.get("status", ""),
                "period": period,
                "start": start_of(period),
                "deadline": deadline_of(period),
                "link": x["_link"] or RIPC_LIST,
                "js": x["_js"],
            }
        )
    return out


# ───────── 2. 수출바우처 ─────────
def scrape_export():
    out = {"items": [], "meta": {}}
    seen = set()
    for page in (1, 2):
        url = EXPORT_LIST + (f"&pageIndex={page}" if page > 1 else "")
        r = get(url)
        save_debug(f"export_list_p{page}", r.text)
        soup = BeautifulSoup(r.text, "html.parser")
        rows = parse_tables(soup, url)
        # 표가 아닌 ul/li 형태일 경우 대비
        if not rows:
            for li in soup.select("ul li"):
                a = li.find("a")
                if not a or len(clean(a.get_text())) < 8:
                    continue
                txt = clean(li.get_text(" "))
                ds = dates_in(txt)
                href = a.get("href", "")
                rows.append(
                    {
                        "title": clean(a.get_text(" ")),
                        "date": ds[0] if ds else "",
                        "_link": urljoin(url, href) if href and not href.startswith(("#", "javascript")) else None,
                        "_js": a.get("onclick") or (href if href.startswith("javascript") else None),
                    }
                )
        for x in rows:
            t = x.get("title")
            if not t or t in seen:
                continue
            seen.add(t)
            period = x.get("period", "")
            # 제목/본문에 접수기간이 있으면 추출
            out["items"].append(
                {
                    "id": x.get("no") or t,
                    "title": t,
                    "type": x.get("type", ""),
                    "date": (dates_in(x.get("date", "")) or [""])[0],
                    "period": period,
                    "deadline": deadline_of(period) if period else None,
                    "notice": not x.get("no", "").isdigit(),
                    "link": x["_link"] or EXPORT_LIST,
                    "js": x["_js"],
                }
            )
    return out


# ───────── 3. 기업마당 ─────────
def bizinfo_url(keyword, page=1):
    params = {
        "rows": 15,
        "cpage": page,
        "schEndAt": "N",  # 마감 공고 제외
        "condition": "searchPblancNm",  # 공고명 검색
        "keyword": keyword,
    }
    return BIZINFO_LIST + "?" + urlencode(params)


def scrape_bizinfo():
    out = {"items": [], "meta": {"keywords": BIZINFO_KEYWORDS}}
    seen = {}
    for kw in BIZINFO_KEYWORDS:
        for page in (1, 2, 3):
            url = bizinfo_url(kw, page)
            r = get(url)
            if page == 1:
                slug = re.sub(r"\W+", "_", kw)
                save_debug("bizinfo_" + slug, r.text)
            rows = [x for x in parse_tables(BeautifulSoup(r.text, "html.parser"), url) if x.get("title")]
            for x in rows:
                link = x["_link"] or url
                m = re.search(r"pblancId=([A-Z_0-9]+)", link)
                key = m.group(1) if m else x["title"]
                if key in seen:
                    if kw not in seen[key]["keywords"]:
                        seen[key]["keywords"].append(kw)
                    continue
                period = x.get("period", "")
                item = {
                    "id": key,
                    "title": x["title"],
                    "type": x.get("type", ""),
                    "org": x.get("org", ""),
                    "period": period,
                    "start": start_of(period),
                    "deadline": deadline_of(period),
                    "date": (dates_in(x.get("date", "")) or [""])[0],
                    "keywords": [kw],
                    "link": (
                        f"https://www.bizinfo.go.kr/sii/siia/selectSIIA200Detail.do?pblancId={key}"
                        if m
                        else link
                    ),
                }
                seen[key] = item
            if len(rows) < 15:
                break
    out["items"] = list(seen.values())
    return out


def scrape_bizinfo_api(key):
    """기업마당 공식 API (선택). BIZINFO_KEY 시크릿이 있으면 이걸 먼저 시도."""
    r = get(BIZINFO_API, params={"crtfcKey": key, "dataType": "json", "searchCnt": 1000})
    data = r.json()
    raw = data.get("jsonArray") or data.get("items") or []
    items = []
    for it in raw:
        title = it.get("pblancNm", "")
        kws = [k for k in BIZINFO_KEYWORDS if k.replace(" ", "") in title.replace(" ", "")]
        if not kws:
            continue
        period = it.get("reqstBeginEndDe", "")
        items.append(
            {
                "id": it.get("pblancId"),
                "title": title,
                "type": it.get("pldirSportRealmLclasCodeNm", ""),
                "org": " / ".join(filter(None, [it.get("jrsdInsttNm"), it.get("excInsttNm")])),
                "period": period,
                "start": start_of(period),
                "deadline": deadline_of(period),
                "date": (dates_in(it.get("creatPnttm", "")) or [""])[0],
                "keywords": kws,
                "link": urljoin("https://www.bizinfo.go.kr", it.get("pblancUrl", "")),
            }
        )
    return {"items": items, "meta": {"keywords": BIZINFO_KEYWORDS, "via": "api"}}


# ───────── 실행 ─────────
def main():
    prev = {}
    if DATA.exists():
        try:
            prev = json.loads(DATA.read_text(encoding="utf-8"))
        except Exception:
            prev = {}

    jobs = {
        "ripc": scrape_ripc,
        "export": scrape_export,
        "bizinfo": scrape_bizinfo,
    }
    biz_key = os.environ.get("BIZINFO_KEY")

    result = {"updated": now_kst().isoformat(timespec="minutes"), "sources": {}}
    today = now_kst().strftime("%Y-%m-%d")

    for name, fn in jobs.items():
        src = {"ok": False, "error": None, "items": [], "meta": {}}
        try:
            if name == "bizinfo" and biz_key:
                try:
                    got = scrape_bizinfo_api(biz_key)
                except Exception:
                    got = fn()
            else:
                got = fn()
            src.update(got)
            src["ok"] = True
            if not got["items"]:
                src["error"] = "목록 0건 (구조 변경 또는 차단 가능성, debug/ 확인)"
        except Exception as e:
            src["error"] = f"{type(e).__name__}: {e}"
            traceback.print_exc()
            # 실패 시 직전 데이터 유지
            old = prev.get("sources", {}).get(name, {})
            src["items"] = old.get("items", [])
            src["meta"] = old.get("meta", {})
            src["stale"] = True

        # 신규 표시용 first_seen 유지
        old_seen = {
            it.get("id"): it.get("first_seen")
            for it in prev.get("sources", {}).get(name, {}).get("items", [])
        }
        for it in src["items"]:
            it["first_seen"] = old_seen.get(it.get("id")) or it.get("first_seen") or today

        print(f"[{name}] ok={src['ok']} items={len(src['items'])} err={src['error']}")
        result["sources"][name] = src

    DATA.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
