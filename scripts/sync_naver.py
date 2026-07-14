#!/usr/bin/env python3
"""
네이버 블로그 RSS → 정적 홈페이지 자동 동기화
- RSS에서 새 글을 읽어 templates/post.html 템플릿으로 변환
- 본문 이미지를 assets/images/posts/ 로 다운로드해 로컬 경로로 교체
- blog/index.html 목록, 홈 최신글 영역, sitemap.xml 자동 갱신
- 처리 이력은 data/posts.json 에 저장 (중복 방지)

사용법:
  python scripts/sync_naver.py
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
from datetime import datetime, timezone
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

ROOT = Path(__file__).resolve().parent.parent
BLOG_DIR = ROOT / "blog"
IMG_DIR = ROOT / "assets" / "images" / "posts"
DATA_FILE = ROOT / "data" / "posts.json"
TEMPLATE_FILE = ROOT / "templates" / "post.html"
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


def download_images(content: str, slug: str, session: requests.Session) -> str:
    """본문 내 네이버 이미지 → 로컬 다운로드 후 경로 교체."""
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    imgs = re.findall(r'<img[^>]+src="([^"]+)"', content)
    for i, src in enumerate(imgs):
        url = html.unescape(src)
        # 네이버 썸네일 파라미터 제거 → 원본 화질
        url = re.sub(r"\?type=w\d+.*$", "", url)
        try:
            r = session.get(url, headers=HEADERS, timeout=20)
            r.raise_for_status()
            ext = os.path.splitext(urlparse(url).path)[1] or ".jpg"
            if len(ext) > 5:
                ext = ".jpg"
            fname = f"{slug}-{i+1}{ext}"
            (IMG_DIR / fname).write_bytes(r.content)
            content = content.replace(src, f"/assets/images/posts/{fname}")
            print(f"    이미지 저장: {fname}")
        except Exception as e:
            print(f"    이미지 다운로드 실패({url[:60]}…): {e}")
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


def build_blog_index(records: list[dict]):
    """blog/index.html 전체 재생성"""
    items_html = "\n".join(
        f'''      <a class="post-item" href="/blog/{r["slug"]}.html">
        <span class="post-date">{r["date_display"]}{(" · " + r["category"]) if r.get("category") else ""}</span>
        <h3>{html.escape(r["title"])}</h3>
        <p>{html.escape(r["description"])}</p>
      </a>'''
        for r in records
    )
    page = f'''<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>번역·행정 가이드 | 루미번역행정사사무소</title>
<meta name="description" content="번역공증, 아포스티유, 국가별 서류 제출 요건 등 실무 가이드 모음. 루미번역행정사사무소.">
<link rel="canonical" href="{SITE_URL}/blog/">
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
    <span class="post-date">GUIDE</span>
    <h1>번역·행정 가이드</h1>
  </div>
</section>
<section class="section">
  <div class="container">
    <div class="post-list">
{items_html}
    </div>
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
    (BLOG_DIR / "index.html").write_text(page, encoding="utf-8")
    print("blog/index.html 갱신 완료")


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


def main():
    BLOG_DIR.mkdir(exist_ok=True)
    DATA_FILE.parent.mkdir(exist_ok=True)
    template = TEMPLATE_FILE.read_text(encoding="utf-8")
    known = json.loads(DATA_FILE.read_text(encoding="utf-8")) if DATA_FILE.exists() else {}

    session = requests.Session()
    new_count = 0

    for post in fetch_rss():
        if post["link"] in known:
            continue
        slug = slugify(post["title"], post["link"])
        print(f"  새 글: {post['title'][:40]} → {slug}.html")
        full = fetch_full_content(post["link"], session)
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
        new_count += 1

    if new_count == 0:
        print("새 글 없음 — 종료")
        return

    DATA_FILE.write_text(json.dumps(known, ensure_ascii=False, indent=2), encoding="utf-8")
    records = sorted(known.values(), key=lambda r: r["date_iso"], reverse=True)
    build_blog_index(records)
    update_home_recent(records)
    build_sitemap(records)
    print(f"완료: 새 글 {new_count}개 게시")


if __name__ == "__main__":
    main()
