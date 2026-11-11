#!/usr/bin/env python3
"""
네이버 블로그 RSS → 정적 홈페이지 자동 동기화
- RSS에서 새 글을 읽어 templates/post.html 템플릿으로 변환
- 본문 이미지를 assets/images/posts/ 로 다운로드해 로컬 경로로 교체
- blog/index.html 목록, 홈 최신글 영역, sitemap.xml 자동 갱신
- 처리 이력은 data/posts.json 에 저장 (중복 방지)

사용법:
  python scripts/sync_naver.py
  (RSS에 없는 오래된 글은 data/backfill.json 에 적어 두면 함께 게시된다)
환경변수 (선택):
  NAVER_BLOG_ID  (기본: lumi_translate)
  SITE_URL       (기본: https://www.lumitrans.co.kr)
"""

import os
import re
import json
import html
import hashlib
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse

import requests

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

# ---------------------------------------------------------------- 설정
BLOG_ID = os.environ.get("NAVER_BLOG_ID", "lumi_translate")
SITE_URL = os.environ.get("SITE_URL", "https://www.lumitrans.co.kr").rstrip("/")
RSS_URL = f"https://rss.blog.naver.com/{BLOG_ID}.xml"

# 홈페이지에 게시하지 않을 카테고리 (쉼표로 구분, 환경변수로 재정의 가능)
def _norm_cat(s: str) -> str:
    """카테고리명 비교용 정규화: 모든 종류의 공백(일반/전각/NBSP 등) 제거"""
    return re.sub(r"[\s\u00a0\u3000]+", "", s or "")

EXCLUDE_CATEGORIES = {
    _norm_cat(c) for c in os.environ.get(
        "EXCLUDE_CATEGORIES", "번역서 소개,오시는 길,번역과 일상"
    ).split(",") if c.strip()
}

ROOT = Path(__file__).resolve().parent.parent
BLOG_DIR = ROOT / "blog"
IMG_DIR = ROOT / "assets" / "images" / "posts"
DATA_FILE = ROOT / "data" / "posts.json"
TEMPLATE_FILE = ROOT / "templates" / "post.html"
BACKFILL_FILE = ROOT / "data" / "backfill.json"
KST = timezone(timedelta(hours=9))
HOME_FILE = ROOT / "index.html"
SITEMAP_FILE = ROOT / "sitemap.xml"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://blog.naver.com/",
}

STATIC_PAGES = [
    "", "services/", "services/notarized-translation.html",
    "services/apostille.html", "services/administrative.html", "contact.html",
]

# ---------------------------------------------------------------- 유틸
def slugify(title: str, link: str) -> str:
    """제목에서 URL용 슬러그 생성. 한글은 그대로 두면 URL 인코딩되므로
    영숫자만 남기고, 비어 있으면 네이버 logNo를 사용."""
    ascii_part = unicodedata.normalize("NFKD", title)
    ascii_part = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_part).strip("-").lower()
    log_no = re.search(r"(\d{6,})", link)
    suffix = log_no.group(1) if log_no else hashlib.md5(link.encode()).hexdigest()[:8]
    if ascii_part and len(ascii_part) >= 4:
        return f"{ascii_part[:60]}-{suffix}"
    return f"post-{suffix}"


def clean_naver_html(content: str) -> str:
    """네이버 특유의 마크업을 정리한다."""
    # 스크립트/스타일 제거
    content = re.sub(r"<script[\s\S]*?</script>", "", content, flags=re.I)
    content = re.sub(r"<style[\s\S]*?</style>", "", content, flags=re.I)
    # 네이버 에디터 잔여 속성 정리
    content = re.sub(r'\s(?:class|id|style|data-[\w-]+)="[^"]*"', "", content)
    # 빈 span/div 제거 (2회 반복으로 중첩 처리)
    for _ in range(2):
        content = re.sub(r"<(span|div)>\s*</\1>", "", content)
    return content.strip()


IMG_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


def download_images(content: str, slug: str, session: requests.Session) -> str:
    """본문 내 네이버 이미지 → 로컬 다운로드 후 경로 교체.
    네이버 이미지 서버는 주소 형식이 까다로워(썸네일 서버는 ?type= 필수 등)
    여러 후보 주소를 순서대로 시도한다: 원본 서버 전환 → 파라미터 제거 →
    고화질 파라미터 → 원래 주소 그대로."""
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    imgs = re.findall(r'<img[^>]+src="([^"]+)"', content)
    for i, src in enumerate(imgs):
        url = html.unescape(src)
        base = re.sub(r"\?.*$", "", url)
        candidates = []
        if "mblogthumb-phinf.pstatic.net" in base:
            # 썸네일 서버 → 원본 서버로 전환 시도 (최고 화질)
            candidates.append(base.replace("mblogthumb-phinf.pstatic.net", "postfiles.pstatic.net"))
        candidates += [base, base + "?type=w966", url]
        # 중복 제거 (순서 유지)
        seen, ordered = set(), []
        for c in candidates:
            if c not in seen:
                seen.add(c); ordered.append(c)
        data, last_err = None, None
        for cand in ordered:
            try:
                r = session.get(cand, headers=HEADERS, timeout=20)
                r.raise_for_status()
                if r.content:
                    data = r.content
                    break
            except Exception as e:
                last_err = e
        if data is None:
            print(f"    이미지 다운로드 실패({url[:60]}…): {last_err}")
            continue
        ext = os.path.splitext(urlparse(base).path)[1].lower()
        if ext not in IMG_EXTS:
            ext = ".jpg"
        fname = f"{slug}-{i+1}{ext}"
        (IMG_DIR / fname).write_bytes(data)
        content = content.replace(src, f"/assets/images/posts/{fname}")
        print(f"    이미지 저장: {fname}")
    return content


def make_description(content: str, limit: int = 150) -> str:
    text = re.sub(r"<[^>]+>", " ", content)
    text = html.unescape(re.sub(r"\s+", " ", text)).strip()
    return text[:limit].rsplit(" ", 1)[0] + ("…" if len(text) > limit else "")


def json_str(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


# ---------------------------------------------------------------- 메인 처리

def fetch_full_content(link: str, session: requests.Session) -> str | None:
    """네이버 RSS가 요약만 제공하므로, 글 페이지에서 본문 전체를 직접 가져온다.
    모바일 페이지(m.blog.naver.com)가 구조가 단순해 우선 시도하고,
    실패 시 PC용 PostView를 시도한다. 둘 다 실패하면 None (RSS 요약 사용)."""
    if BeautifulSoup is None:
        print("    bs4 미설치 — RSS 요약 사용")
        return None
    m = re.search(r"/(\d{6,})", link)
    if not m:
        return None
    log_no = m.group(1)
    candidates = [
        f"https://m.blog.naver.com/{BLOG_ID}/{log_no}",
        f"https://blog.naver.com/PostView.naver?blogId={BLOG_ID}&logNo={log_no}",
    ]
    for url in candidates:
        try:
            r = session.get(url, headers=HEADERS, timeout=25)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")
            container = (
                soup.select_one("div.se-main-container")   # 스마트에디터 ONE
                or soup.select_one("div#postViewArea")      # 구버전 에디터
                or soup.select_one("div.post_ct")           # 모바일 구버전
            )
            if not container:
                continue
            # 지연 로딩 이미지 실제 주소로 교체
            for img in container.find_all("img"):
                lazy = img.get("data-lazy-src") or img.get("data-src")
                if lazy:
                    img["src"] = lazy
                # 광고/스티커 등 src 없는 이미지 제거
                if not img.get("src"):
                    img.decompose()
            # 스크립트·불필요 요소 제거
            for tag in container.select("script, style, .se-oglink, .se_ad, .naver-splugin"):
                tag.decompose()
            html_out = str(container)
            if len(re.sub(r"<[^>]+>", "", html_out).strip()) > 50:
                print(f"    본문 전체 수집 성공 ({url.split('/')[2]})")
                return html_out
        except Exception as e:
            print(f"    본문 수집 실패({url[:50]}…): {e}")
    return None


def fetch_rss() -> list[dict]:
    print(f"RSS 요청: {RSS_URL}")
    r = requests.get(RSS_URL, headers=HEADERS, timeout=30)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    items = []
    for item in root.iter("item"):
        get = lambda tag: (item.findtext(tag) or "").strip()
        pub = get("pubDate")
        try:
            dt = parsedate_to_datetime(pub)
        except Exception:
            dt = datetime.now(timezone.utc)
        items.append({
            "title": html.unescape(get("title")),
            "link": get("link"),
            "category": get("category"),
            "content": get("description"),
            "date": dt,
        })
    print(f"RSS에서 {len(items)}개 글 발견")
    return items


def render_post(post: dict, slug: str, template: str) -> str:
    date_iso = post["date"].strftime("%Y-%m-%dT%H:%M:%S%z") or post["date"].isoformat()
    date_display = post["date"].strftime("%Y. %m. %d")
    cat = f" · {post['category']}" if post.get("category") else ""
    desc = make_description(post["content"])
    out = template
    for k, v in {
        "{{TITLE}}": html.escape(post["title"]),
        "{{TITLE_JSON}}": json_str(post["title"]),
        "{{DESCRIPTION}}": html.escape(desc),
        "{{DESCRIPTION_JSON}}": json_str(desc),
        "{{SLUG}}": slug,
        "{{SITE_URL}}": SITE_URL,
        "{{DATE_ISO}}": date_iso,
        "{{DATE_DISPLAY}}": date_display,
        "{{CATEGORY_SUFFIX}}": html.escape(cat),
        "{{ORIGINAL_URL}}": post["link"],
        "{{CONTENT}}": post["content"],
    }.items():
        out = out.replace(k, v)
    return out


# ---------------------------------------------------------------- 분류(카테고리) 페이지
def fix_cat(cat: str) -> str:
    """네이버 RSS가 분류명을 중간에 자르는 경우(닫는 괄호 누락)를 보정"""
    cat = (cat or "").strip()
    return cat + ")" * max(0, cat.count("(") - cat.count(")"))


# 분류명 → (URL 슬러그, 분류 소개 문장). 새 분류가 생기면 여기에 추가한다.
# 목록에 없는 분류는 자동으로 슬러그를 만들고 기본 소개 문장을 사용한다.
CATEGORY_INFO = {
    "법원서류(판결문/약식명령/소장)": ("court-documents",
        "판결문, 약식명령, 소장, 불기소 결정서 등 법원과 수사기관 문서를 해외 기관에 제출하기 위해 번역한 사례와 안내를 모았습니다."),
    "계약서/관공서제출서류": ("contracts-official-documents",
        "계약서, 부동산 등기부등본, 사망증명서, 세무서 발급 증명서 등 계약서와 관공서 제출 서류의 번역 사례와 안내를 모았습니다."),
    "의료문서번역": ("medical-documents",
        "진단서, 처방전, 검사지, 진료기록 등 의료 문서의 번역 사례와 안내를 모았습니다."),
    "유학/이민/취업/비자": ("study-immigration-visa",
        "유학, 이민, 취업, 비자 신청에 필요한 증명서와 거래내역서 등의 번역 사례와 안내를 모았습니다."),
    "기업서류(정관/재무/총회)": ("corporate-documents",
        "법인등기부등본, 정관, 이사회 의사록, 계약서 등 기업서류의 번역공증과 아포스티유 사례, 안내를 모았습니다."),
    "성적/졸업/생활기록부": ("school-records",
        "생활기록부, 졸업증명서, 성적증명서, 해외대학 입학통지서와 합격통지서 등 학력 서류의 번역 사례와 안내를 모았습니다. 국제학교와 해외 유학을 준비하실 때 참고해 주세요."),
    "논문번역": ("thesis-translation",
        "논문 초록과 게재확인서 등 학술 서류의 번역 사례를 모았습니다."),
    "아포스티유/영사확인": ("apostille-consular",
        "아포스티유와 영사확인 절차, 그리고 해당 서류의 번역공증 사례와 안내를 모았습니다."),
    "가족/기본/혼인관계": ("family-relations",
        "가족관계증명서, 기본증명서, 혼인관계증명서 등 가족관계 서류의 번역과 번역확인증명서 발급 사례를 모았습니다."),
    "번역행정사(영어)": ("translator-english",
        "번역행정사가 수행하는 영문 번역 업무의 사례와 안내를 모았습니다."),
    "번역확인증명(번역자확인서)": ("translation-certification",
        "번역확인증명서(번역자 확인서)의 의미와 제출처, 발급 사례를 모았습니다."),
    "번역공증촉탁대행": ("notarization-agency",
        "번역공증 촉탁 대행과 관련한 안내와 소식을 모았습니다."),
}
_CAT_LOOKUP = {_norm_cat(k): (k, v[0], v[1]) for k, v in CATEGORY_INFO.items()}


def cat_meta(cat: str):
    """분류명 → (표시명, 슬러그, 소개 문장). 빈 분류는 None."""
    cat = fix_cat(cat)
    if not cat:
        return None
    hit = _CAT_LOOKUP.get(_norm_cat(cat))
    if hit:
        return hit
    slug = "cat-" + hashlib.md5(_norm_cat(cat).encode()).hexdigest()[:6]
    return (cat, slug, f"{cat} 분류의 번역 사례와 안내를 모았습니다.")


def group_by_category(records: list[dict]) -> dict:
    groups = {}
    for r in records:
        m = cat_meta(r.get("category", ""))
        if not m:
            continue
        g = groups.setdefault(m[1], {"name": m[0], "slug": m[1], "intro": m[2], "records": []})
        g["records"].append(r)
    return groups


def post_items_html(records: list[dict]) -> str:
    return "\n".join(
        f'''      <a class="post-item" href="/blog/{r["slug"]}.html">
        <span class="post-date">{r["date_display"]}{(" · " + fix_cat(r["category"])) if r.get("category") else ""}</span>
        <h3>{html.escape(r["title"])}</h3>
        <p>{html.escape(r["description"])}</p>
      </a>'''
        for r in records
    )


def category_nav_html(groups: dict, active: str | None) -> str:
    base = "display:inline-block;padding:6px 14px;border:1px solid #cdbf9f;border-radius:999px;font-size:0.9rem;text-decoration:none;"
    on = "background:#16213a;color:#fff;border-color:#16213a;"
    off = "background:#fff;color:inherit;"
    total = sum(len(g["records"]) for g in groups.values())
    chips = [f'<a href="/blog/" style="{base}{on if active is None else off}">전체</a>']
    for g in sorted(groups.values(), key=lambda g: -len(g["records"])):
        label = html.escape(g["name"].split("(")[0])
        style = base + (on if g["slug"] == active else off)
        chips.append(f'<a href="/blog/category/{g["slug"]}.html" style="{style}">{label} ({len(g["records"])})</a>')
    return ('    <nav aria-label="분류" style="display:flex;flex-wrap:wrap;gap:8px;margin-bottom:28px;">\n      '
            + "\n      ".join(chips) + "\n    </nav>")


def page_shell(title: str, description: str, canonical: str, label: str, h1: str, inner: str) -> str:
    return f'''<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{html.escape(title)}</title>
<meta name="description" content="{html.escape(description)}">
<link rel="canonical" href="{canonical}">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@400;500;700&family=Noto+Serif+KR:wght@600;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/assets/css/style.css">
</head>
<body>
<header class="site-header">
  <div class="header-inner">
    <a class="brand" href="/"><span class="seal">Lumi</span>루미번역행정사사무소</a>
    <button class="nav-toggle" aria-label="메뉴 열기" onclick="document.querySelector('.main-nav').classList.toggle('open')">☰</button>
    <nav class="main-nav" aria-label="주 메뉴">
      <a href="/">홈</a>
      <a href="/services/">서비스</a>
      <a href="/blog/" class="active">번역·행정 가이드</a>
      <a href="/contact.html">오시는 길·문의</a>
    </nav>
  </div>
</header>
<main>
<section class="post-header">
  <div class="container">
    <span class="post-date">{html.escape(label)}</span>
    <h1>{html.escape(h1)}</h1>
  </div>
</section>
<section class="section">
  <div class="container">
{inner}
  </div>
</section>
</main>
<footer class="site-footer">
  <div class="container">
    <div class="footer-bottom">© 2026 루미번역행정사사무소 · <a href="/">홈</a> · <a href="/services/">서비스</a> · <a href="/contact.html">문의</a></div>
  </div>
</footer>
</body>
</html>
'''


def build_blog_index(records: list[dict]):
    """blog/index.html 전체 재생성 (분류 버튼 포함)"""
    groups = group_by_category(records)
    inner = (category_nav_html(groups, None) + "\n"
             + '    <div class="post-list">\n' + post_items_html(records) + "\n    </div>")
    page = page_shell(
        "번역·행정 가이드 | 루미번역행정사사무소",
        "번역공증, 아포스티유, 국가별 서류 제출 요건 등 실무 가이드 모음. 루미번역행정사사무소.",
        f"{SITE_URL}/blog/", "GUIDE", "번역·행정 가이드", inner)
    (BLOG_DIR / "index.html").write_text(page, encoding="utf-8")
    print("blog/index.html 갱신 완료")


def build_category_pages(records: list[dict]):
    """blog/category/<슬러그>.html 분류별 페이지 전체 재생성"""
    groups = group_by_category(records)
    cat_dir = BLOG_DIR / "category"
    cat_dir.mkdir(exist_ok=True)
    keep = set()
    for g in groups.values():
        short = g["name"].replace("/", "·")
        inner = (category_nav_html(groups, g["slug"]) + "\n"
                 + f'    <p style="margin-bottom:28px;max-width:760px;">{html.escape(g["intro"])}</p>\n'
                 + '    <div class="post-list">\n' + post_items_html(g["records"]) + "\n    </div>")
        page = page_shell(
            f"{short} 번역 사례와 안내 | 루미번역행정사사무소",
            g["intro"],
            f'{SITE_URL}/blog/category/{g["slug"]}.html',
            "CATEGORY", short, inner)
        (cat_dir / f'{g["slug"]}.html').write_text(page, encoding="utf-8")
        keep.add(f'{g["slug"]}.html')
    for old in cat_dir.glob("*.html"):   # 더 이상 글이 없는 분류 페이지 정리
        if old.name not in keep:
            old.unlink()
    print(f"분류 페이지 {len(groups)}개 갱신 완료")


def update_home_recent(records: list[dict]):
    """홈페이지 최신 글 3개 영역 갱신 (SYNC 마커 사이 교체)"""
    if not HOME_FILE.exists():
        return
    recent = records[:3]
    block = "\n".join(
        f'''      <a class="post-item" href="/blog/{r["slug"]}.html">
        <span class="post-date">{r["date_display"]}</span>
        <h3>{html.escape(r["title"])}</h3>
        <p>{html.escape(r["description"])}</p>
      </a>'''
        for r in recent
    )
    src = HOME_FILE.read_text(encoding="utf-8")
    src = re.sub(
        r"(<!-- SYNC:RECENT_POSTS_START -->)[\s\S]*?(<!-- SYNC:RECENT_POSTS_END -->)",
        rf"\1\n{block}\n      \2",
        src,
    )
    HOME_FILE.write_text(src, encoding="utf-8")
    print("index.html 최신 글 영역 갱신 완료")


def build_sitemap(records: list[dict]):
    today = datetime.now().strftime("%Y-%m-%d")
    urls = []
    for p in STATIC_PAGES:
        urls.append(f"  <url><loc>{SITE_URL}/{p}</loc><lastmod>{today}</lastmod></url>")
    urls.append(f"  <url><loc>{SITE_URL}/blog/</loc><lastmod>{today}</lastmod></url>")
    for g in group_by_category(records).values():
        last = max(r["date_iso"][:10] for r in g["records"])
        urls.append(f"  <url><loc>{SITE_URL}/blog/category/{g['slug']}.html</loc><lastmod>{last}</lastmod></url>")
    for r in records:
        urls.append(
            f"  <url><loc>{SITE_URL}/blog/{r['slug']}.html</loc>"
            f"<lastmod>{r['date_iso'][:10]}</lastmod></url>"
        )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(urls) + "\n</urlset>\n"
    )
    SITEMAP_FILE.write_text(xml, encoding="utf-8")
    print("sitemap.xml 갱신 완료")



def cleanup_excluded(known: dict) -> int:
    """이미 게시된 글 중 제외 카테고리에 해당하는 것을 소급 제거한다."""
    removed = 0
    for link, rec in list(known.items()):
        if rec.get("excluded"):
            continue
        if _norm_cat(rec.get("category", "")) in EXCLUDE_CATEGORIES:
            slug = rec.get("slug", "")
            if slug:
                f = BLOG_DIR / f"{slug}.html"
                if f.exists():
                    f.unlink()
                for img in IMG_DIR.glob(f"{slug}-*"):
                    img.unlink()
            print(f"  기존 게시글 내림(제외 카테고리 '{rec.get('category','')}'): {rec.get('title','')[:36]}")
            known[link] = {"excluded": True, "category": rec.get("category", "")}
            removed += 1
    return removed


def add_post(post: dict, known: dict, session: requests.Session, template: str,
             require_full: bool = False) -> bool:
    """글 1개를 게시한다. 게시했으면 True. (이미 있거나 제외 카테고리면 False)
    require_full=True 이면 본문 전체를 못 가져왔을 때 게시하지 않는다(오래된 글 가져오기용)."""
    if post["link"] in known:
        return False
    if _norm_cat(post.get("category", "")) in EXCLUDE_CATEGORIES:
        print(f"  제외(카테고리: {post['category']}): {post['title'][:36]}")
        known[post["link"]] = {"excluded": True, "category": post["category"]}
        return False
    slug = slugify(post["title"], post["link"])
    print(f"  새 글: {post['title'][:40]} → {slug}.html")
    full = fetch_full_content(post["link"], session)
    if require_full and not full:
        print("    본문을 가져오지 못해 건너뜀 (다음 실행 때 다시 시도)")
        return False
    raw = full if full else post["content"]
    if not full:
        print("    (RSS 요약으로 대체)")
    content = clean_naver_html(raw)
    content = download_images(content, slug, session)
    post["content"] = content
    (BLOG_DIR / f"{slug}.html").write_text(render_post(post, slug, template), encoding="utf-8")
    known[post["link"]] = {
        "slug": slug,
        "title": post["title"],
        "category": post.get("category", ""),
        "description": make_description(content, 110),
        "date_iso": post["date"].isoformat(),
        "date_display": post["date"].strftime("%Y. %m. %d"),
    }
    return True


def process_backfill(known: dict, session: requests.Session, template: str) -> int:
    """data/backfill.json 에 적힌, RSS에 더 이상 나오지 않는 오래된 글을 게시한다.
    항목 형식: {"logNo": "224123456789", "title": "...", "date": "2026-03-20", "category": "..."}
    (link 를 직접 적어도 된다). RSS가 주는 주소 형식과 맞춰 저장하므로 중복 게시되지 않는다."""
    if not BACKFILL_FILE.exists():
        return 0
    entries = json.loads(BACKFILL_FILE.read_text(encoding="utf-8"))
    count = 0
    for e in entries:
        m = re.search(r"(\d{6,})", str(e.get("logNo") or e.get("link") or ""))
        if not m or not e.get("title") or not e.get("date"):
            print(f"  backfill 항목 건너뜀(logNo/title/date 확인 필요): {e}")
            continue
        link = f"https://blog.naver.com/{BLOG_ID}/{m.group(1)}?fromRss=true&trackingCode=rss"
        dt = datetime.strptime(e["date"], "%Y-%m-%d").replace(hour=12, tzinfo=KST)
        post = {"title": e["title"], "link": link, "category": e.get("category", ""),
                "content": "", "date": dt}
        if add_post(post, known, session, template, require_full=True):
            count += 1
    return count


def main():
    BLOG_DIR.mkdir(exist_ok=True)
    DATA_FILE.parent.mkdir(exist_ok=True)
    template = TEMPLATE_FILE.read_text(encoding="utf-8")
    known = json.loads(DATA_FILE.read_text(encoding="utf-8")) if DATA_FILE.exists() else {}

    removed = cleanup_excluded(known)

    session = requests.Session()
    new_count = 0

    for post in fetch_rss():
        if add_post(post, known, session, template):
            new_count += 1

    new_count += process_backfill(known, session, template)

    DATA_FILE.write_text(json.dumps(known, ensure_ascii=False, indent=2), encoding="utf-8")
    force = os.environ.get("FORCE_REBUILD") == "1" or not (BLOG_DIR / "category").exists()
    if new_count == 0 and removed == 0 and not force:
        print("변경 사항 없음 — 종료")
        return

    records = sorted([r for r in known.values() if not r.get("excluded")], key=lambda r: r["date_iso"], reverse=True)
    build_blog_index(records)
    build_category_pages(records)
    update_home_recent(records)
    build_sitemap(records)
    print(f"완료: 새 글 {new_count}개 게시, 기존 글 {removed}개 내림")


if __name__ == "__main__":
    main()
