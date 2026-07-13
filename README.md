# 루미번역행정사사무소 홈페이지

네이버 블로그(`blog.naver.com/lumi_translate`)와 **매일 자동 동기화**되는 정적 홈페이지입니다.
네이버에 글을 쓰면, 다음 날 아침 7시(KST)에 홈페이지에 자동으로 게시됩니다.

---

## 0. 시작 전 — 네이버 블로그 설정 (1회, 필수)

1. 네이버 블로그 → **관리** → **기본 설정** → **기본 정보 관리 > 블로그 정보**
2. **RSS 공개**를 **"전체공개"** 로 설정
   - "요약공개"면 글 앞부분만 홈페이지로 넘어옵니다.

## 1. GitHub 계정 만들기 & 코드 올리기

1. https://github.com 가입 (무료)
2. 우측 상단 **+** → **New repository** → 이름: `lumi-site`, **Public** 선택 → Create
3. 이 폴더의 파일 전체를 업로드:
   - 저장소 페이지에서 **uploading an existing file** 클릭 → 폴더 내용물 전체 드래그 → Commit
   - 또는 Git 사용 시:
     ```bash
     git remote add origin https://github.com/본인아이디/lumi-site.git
     git add -A && git commit -m "initial" && git push -u origin main
     ```
   - 주의: 숨김 폴더 `.github`(자동화 설정)가 반드시 함께 올라가야 합니다.

## 2. 무료 호스팅 켜기 (GitHub Pages)

1. 저장소 → **Settings** → **Pages**
2. Source: **Deploy from a branch** → Branch: `main` / `(root)` → Save
3. 1~2분 후 `https://본인아이디.github.io/lumi-site/` 에서 사이트 확인 가능

## 3. 첫 동기화 실행

1. 저장소 → **Actions** 탭 → 좌측 "네이버 블로그 자동 동기화" 클릭
2. **Run workflow** 버튼 → 실행
3. 1~2분 뒤 네이버 최근 글(최대 50개)이 `/blog/`에 생성됩니다
4. 이후에는 **매일 오전 7시 자동 실행** — 아무것도 안 하셔도 됩니다

## 4. 도메인 연결

1. 가비아(gabia.com) 또는 Cloudflare에서 도메인 구매 (예: `lumitrans.co.kr`)
2. DNS 설정에서 다음 레코드 추가:
   - `A` 레코드 4개: `185.199.108.153`, `185.199.109.153`, `185.199.110.153`, `185.199.111.153`
   - `CNAME` 레코드: `www` → `본인아이디.github.io`
3. 저장소 **Settings → Pages → Custom domain**에 도메인 입력, **Enforce HTTPS** 체크

## 5. 도메인 확정 후 — 파일 내 주소 일괄 교체 (1회)

모든 파일의 `YOURDOMAIN.com`을 실제 도메인으로 교체합니다:
```bash
grep -rl "YOURDOMAIN.com" . --include="*.html" --include="*.py" --include="*.yml" --include="*.txt" \
  | xargs sed -i 's/www.YOURDOMAIN.com/www.실제도메인.co.kr/g'
```
GitHub 웹에서 직접 할 경우: 각 파일 편집 화면에서 찾아 바꾸기.

또한 다음 실제 정보를 입력하세요 (검색: `[상세주소 입력]`, `[전화번호 입력]`, `[이메일 입력]`):
- `index.html`, `contact.html` — 주소·전화·이메일·카카오 채널
- `index.html`의 JSON-LD(LocalBusiness) — telephone, streetAddress, postalCode

## 6. Google 검색 등록

1. https://search.google.com/search-console 접속 → 속성 추가 → 도메인 입력
2. 안내에 따라 DNS TXT 레코드로 소유권 확인
3. **Sitemaps** 메뉴 → `https://도메인/sitemap.xml` 제출
4. (선택) 네이버 서치어드바이저(searchadvisor.naver.com)에도 동일하게 등록

## 구조

```
├── index.html                  # 홈 (LocalBusiness 스키마)
├── services/                   # 서비스 상세 (Service + FAQ 스키마)
├── blog/                       # 자동 생성되는 블로그 글
├── contact.html
├── assets/css/style.css        # 네이비/골드/크림 디자인 시스템
├── assets/images/posts/        # 자동 다운로드된 본문 이미지
├── templates/post.html         # 글 템플릿 (Article 스키마)
├── scripts/sync_naver.py       # RSS 동기화 스크립트
├── data/posts.json             # 처리 이력 (자동 생성)
├── .github/workflows/sync.yml  # 매일 자동 실행 설정
├── robots.txt / sitemap.xml
```

## 자주 묻는 문제

- **글 본문이 잘려서 넘어옴** → 네이버 RSS 공개 설정이 "요약"임. "전체공개"로 변경 후 해당 글을 수정·재발행하면 다음 동기화 때 반영.
- **이미지가 안 보임** → Actions 로그에서 "이미지 다운로드 실패" 확인. 네이버가 간헐적으로 차단하는 경우 워크플로 재실행.
- **과거 글(50개 이전)** → RSS에는 최근 글만 담기므로, 오래된 글은 HTML을 `blog/`에 수동 추가하거나 별도 이전 작업 필요.
