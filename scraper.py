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
RIPC_BASE = "https://pms.ripc.org"
RIPC_API = RIPC_BASE + "/pms/biz/applicant/notice/getNoticeList.do"
RIPC_STATUS = {"NST001": "공고중", "NST002": "작성중", "NST003": "공고종료"}


def ripc_post(url, data, tries=4):
    """응답이 커서 중간에 끊기는 경우가 있어 재시도."""
    import time
    last = None
    for i in range(tries):
        try:
            r = S.post(url, data=data, timeout=60,
                       headers={"Referer": RIPC_LIST, "X-Requested-With": "XMLHttpRequest"})
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            time.sleep(2 + i * 3)
    raise last


PAGE = 20  # 공고 1건이 약 35KB라 작게 나눠 받음


def scrape_ripc():
    """목록은 getNoticeList.do(JSON)로 채워짐. 올해 + 내년 사업연도 조회."""
    out = {"items": [], "meta": {}}
    r = get(RIPC_LIST)  # 세션 쿠키
    save_debug("ripc_list", r.text)
    year = now_kst().year
    seen, errors = set(), []
    for biz_year in (year, year + 1):
        page = 1
        while page <= 30:
            try:
                data = ripc_post(RIPC_API, {
                    "noticeTitle": "", "bizYear": biz_year, "centerCode": "", "bizCategory1": "",
                    "recordCountPerPage": PAGE, "currentPageNo": page,
                    "noticeGroupCode": "NGR001", "noticeCode": "NST000",
                })
            except Exception as e:
                errors.append(f"{biz_year}년 {page}p: {type(e).__name__}")
                page += 1
                continue
            res = data.get("result") or {}
            rows = res.get("noticeList") or []
            if page == 1 and biz_year == year and rows:
                (DEBUG / "ripc_api_sample.json").write_text(
                    json.dumps({k: rows[0].get(k) for k in (
                        "noticeNo", "centerName", "bizCategory1Name", "noticeTitle",
                        "startDateStr", "endDateStr", "noticeCode", "noticeSeq", "bizYear")},
                        ensure_ascii=False, indent=1), encoding="utf-8")
            for n in rows:
                if n.get("noticeCode") == "NST002":
                    continue
                key = f"{n.get('bizYear')}-{n.get('noticeSeq')}"
                if key in seen:
                    continue
                seen.add(key)
                period = f"{n.get('startDateStr', '')} ~ {n.get('endDateStr', '')}"
                out["items"].append({
                    "id": key,
                    "no": n.get("noticeNo", ""),
                    "title": clean(n.get("noticeTitle")),
                    "type": clean(n.get("bizCategory1Name")),
                    "center": clean(n.get("centerName")),
                    "status": RIPC_STATUS.get(n.get("noticeCode"), ""),
                    "period": period,
                    "start": start_of(period),
                    "deadline": deadline_of(period),
                    "link": f"{RIPC_BASE}/pms/biz/applicant/notice/info.do?noticeSeq={n.get('noticeSeq')}&bizYear={n.get('bizYear')}",
                })
            total = int(res.get("totalRecordCount") or 0)
            if page * PAGE >= total or not rows:
                break
            page += 1
    if errors and not out["items"]:
        raise RuntimeError("RIPC 전부 실패: " + ", ".join(errors[:3]))
    if errors:
        out["meta"]["partial_errors"] = errors
    return out


# ───────── 2. 수출바우처 ─────────
def scrape_export():
    out = {"items": [], "meta": {}}
    seen = set()
    for page in (1, 2):
        url = EXPORT_LIST + (f"&pageNo={page}" if page > 1 else "")
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
            m = re.search(r"goDetail\((\d+)\)", x.get("_js") or "")
            nid = m.group(1) if m else None
            out["items"].append(
                {
                    "id": nid or t,
                    "title": t,
                    "type": x.get("type", ""),
                    "date": (dates_in(x.get("date", "")) or [""])[0],
                    "period": period,
                    "deadline": deadline_of(period) if period else None,
                    "notice": not x.get("no", "").isdigit(),
                    "link": x["_link"] or (
                        f"https://www.exportvoucher.com/portal/board/boardView?bbs_id=1&ntt_id={nid}"
                        if nid else EXPORT_LIST),
                    "js": x["_js"],
                }
            )
    return out


# ───────── 3. 기업마당 ─────────
def bizinfo_params(keyword, page=1):
    # 원래 링크의 파라미터를 빈 값까지 그대로 전달 (빠지면 서버가 500을 냄)
    return {
        "hashCode": "", "rowsSel": "6", "rows": "15", "cpage": str(page),
        "cat": "", "article_seq": "", "pblancId": "", "schPblancDiv": "",
        "schJrsdCodeTy": "", "schWntyAt": "", "schAreaDetailCodes": "",
        "schEndAt": "N",  # 마감 공고 제외
        "orderGb": "", "sort": "",
        "condition": "searchPblancNm",  # 공고명 검색
        "condition1": "AND", "preKeywords": "", "keyword": keyword,
    }


def bizinfo_fetch(keyword, page, slug):
    """GET 실패 시 POST로 재시도. 실패 응답도 debug에 저장."""
    params = bizinfo_params(keyword, page)
    hdr = {"Referer": BIZINFO_LIST}
    last = None
    for method in ("GET", "POST"):
        try:
            if method == "GET":
                r = S.get(BIZINFO_LIST, params=params, headers=hdr, timeout=30)
            else:
                r = S.post(BIZINFO_LIST, data=params, headers=hdr, timeout=30)
            r.encoding = r.encoding if r.encoding and r.encoding.lower() != "iso-8859-1" else r.apparent_encoding
            if page == 1:
                save_debug(f"bizinfo_{slug}_{method}_{r.status_code}", r.text)
            if r.ok:
                return r
            last = f"{method} {r.status_code}"
        except Exception as e:
            last = f"{method} {type(e).__name__}: {e}"
    raise RuntimeError(last)


def scrape_bizinfo():
    out = {"items": [], "meta": {"keywords": BIZINFO_KEYWORDS}}
    # 세션 쿠키 먼저 받기
    try:
        S.get("https://www.bizinfo.go.kr/", timeout=30)
        S.get(BIZINFO_LIST, timeout=30)
    except Exception:
        pass
    seen, errors = {}, []
    for kw in BIZINFO_KEYWORDS:
        slug = re.sub(r"\W+", "_", kw)
        for page in (1, 2, 3):
            try:
                r = bizinfo_fetch(kw, page, slug)
            except Exception as e:
                if page == 1:
                    errors.append(f"{kw}: {e}")
                break
            url = r.url
            rows = [x for x in parse_tables(BeautifulSoup(r.text, "html.parser"), url) if x.get("title")]
            for x in rows:
                link = x["_link"] or url
                m = re.search(r"pblancId=([A-Z_0-9]+)", link + " " + (x.get("_js") or ""))
                key = m.group(1) if m else x["title"]
                if key in seen:
                    if kw not in seen[key]["keywords"]:
                        seen[key]["keywords"].append(kw)
                    continue
                period = x.get("period", "")
                seen[key] = {
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
                        if m else link
                    ),
                }
            if len(rows) < 15:
                break
    if errors and len(errors) == len(BIZINFO_KEYWORDS):
        raise RuntimeError("전 키워드 실패 - " + errors[0])
    out["items"] = list(seen.values())
    if errors:
        out["meta"]["partial_errors"] = errors
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
            elif got.get("meta", {}).get("partial_errors"):
                src["error"] = f"일부 페이지 수집 실패 ({len(got['meta']['partial_errors'])}건), 다음 수집 때 다시 시도"
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
