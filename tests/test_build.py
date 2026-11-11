"""사이트 빌드 오프라인 테스트. 실행: python tests/test_build.py"""
import json
import re
import sys
import tempfile
from datetime import date
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import build  # noqa: E402

CFG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
TODAY = date(2026, 11, 11)
SITE = "https://site.test"


def md_post(slug, title, region="전국", topic="지원금", deadline="", published="2026-11-01", updated=None, extra=""):
    fm = yaml.safe_dump({"title": title, "slug": slug, "region": region, "topic": topic, "deadline": deadline,
                         "published": published, "updated": updated or published}, allow_unicode=True, sort_keys=False)
    body = (f"## 한 줄 요약\n\n요약 {slug} 입니다.\n\n## 누가 받을 수 있나\n\n대상입니다.\n\n"
            "## 얼마나 받나\n\n금액은 다음과 같습니다.\n- 가\n- 나\n\n## 언제까지 신청하나\n\n기간입니다.\n\n"
            "## 어떻게 신청하나\n\n방법입니다.\n\n## 자주 묻는 질문\n\n### Q1. 질문\n\n답입니다.\n\n## 문의처\n\n문의입니다.\n\n"
            f"## 참고 기사\n\n- [경향신문](https://k.example/1) · 2026-11-01\n{extra}")
    return f"---\n{fm}---\n\n{body}"


def make_site(tmp: Path, posts: dict[str, str], pages=True, cfg=None, guides: dict[str, str] | None = None) -> tuple[Path, dict]:
    c = tmp / "content"
    (c / "posts").mkdir(parents=True)
    for name, text in posts.items():
        (c / "posts" / f"{name}.md").write_text(text, encoding="utf-8")
    if pages:
        (c / "pages").mkdir()
        (c / "pages" / "about.md").write_text("---\ntitle: 소개\nslug: about\nupdated: 2026-11-01\n---\n\n## 정정 요청\n\n연락처: {{ contact }}\n", encoding="utf-8")
    if guides:
        (c / "guide").mkdir()
        for name, text in guides.items():
            (c / "guide" / f"{name}.md").write_text(text, encoding="utf-8")
    out = tmp / "dist"
    stats = build.build(cfg or CFG, c, out, TODAY, SITE)
    return out, stats


def read(out: Path, rel: str) -> str:
    return (out / rel).read_text(encoding="utf-8")


def test_deadline_status():
    s = build.deadline_status
    assert s(None, TODAY, "지원금")["state"] == "none"
    assert s(date(2026, 11, 10), TODAY, "지원금") == {"state": "closed", "label": "신청 마감", "days": -1}
    assert s(date(2026, 11, 10), TODAY, "세금")["label"] == "적용 종료"          # 신청이 없는 분야는 '적용 종료'
    assert s(date(2026, 11, 11), TODAY, "지원금") == {"state": "soon", "label": "오늘 마감", "days": 0}
    assert s(date(2026, 11, 18), TODAY, "지원금")["state"] == "soon"            # 7일 이내
    assert s(date(2026, 11, 19), TODAY, "지원금") == {"state": "open", "label": "D-8", "days": 8}


def test_list_spacing_and_escaping():
    assert build.fix_list_spacing("문단\n- a\n- b") == "문단\n\n- a\n- b"           # 목록 앞 빈 줄 보정, 항목 사이는 그대로
    assert build.fix_list_spacing("## 제목\n- a") == "## 제목\n- a"
    h = build.render_md("문단\n- a\n- b\n\n<script>alert(1)</script>")
    assert "<ul>" in h and h.count("<li>") == 2 and "<script>" not in h and "&lt;script&gt;" in h


def test_build_pages_and_rules():
    with tempfile.TemporaryDirectory() as t:
        posts = {
            "a-closed": md_post("a-closed", "마감된 사업 (2026)", region="경기", deadline="2026-11-05", published="2026-11-10", updated="2026-11-10"),
            "b-open": md_post("b-open", "접수 중 사업 (2026)", region="경기", deadline="2026-12-31", published="2026-10-01"),
            "c-soon": md_post("c-soon", "곧 마감 (2026)", region="서울", deadline="2026-11-13", published="2026-10-02"),
            "d-tax": md_post("d-tax", "세금 인하 (2027)", topic="세금", deadline="2026-11-01", published="2026-10-03"),
            "e-none": md_post("e-none", "마감일 없는 사업 (2026)", region="경기", published="2026-10-04"),
        }
        out, stats = make_site(Path(t), posts)
        assert stats["posts"] == 5 and stats["closed_posts"] == 2

        # 마감 배지: 지난 글은 삭제·noindex 가 아니라 배지를 단다
        closed = read(out, "post/a-closed/index.html")
        assert "신청 마감" in closed and 'name="robots"' not in closed
        assert "적용 종료" in read(out, "post/d-tax/index.html")
        assert "D-2" in read(out, "post/c-soon/index.html")

        # 목록: 마감된 글은 하단으로. 마감 임박 페이지는 가까운 순.
        idx = read(out, "index.html")
        latest = idx[idx.index("새로 올라온 글"):]
        order = [latest.index(x) for x in ("접수 중 사업", "곧 마감", "마감일 없는 사업", "마감된 사업", "세금 인하")]
        assert order[0] < order[3] and order[1] < order[3] and order[2] < order[3] and order[2] < order[4], order
        dl = read(out, "deadline/index.html")
        assert dl.index("곧 마감") < dl.index("접수 중 사업") < dl.index("마감된 글") < dl.index("마감된 사업 (2026)")
        assert "마감일 없는 사업" not in dl

        # 씬 콘텐츠: 글 3건 미만인 지역·분야는 noindex + 사이트맵 제외, 3건 이상은 색인
        assert 'content="noindex,follow"' in read(out, "region/서울/index.html")
        assert 'noindex' in read(out, "topic/세금/index.html")
        sm = read(out, "sitemap.xml")
        assert "/region/%EA%B2%BD%EA%B8%B0/" in sm or "region/경기" in sm  # 경기 3건(a,b,e) → 색인·사이트맵 포함
        assert 'noindex' not in read(out, "region/경기/index.html")
        assert "region/%EC%84%9C%EC%9A%B8" not in sm and "/topic/%EC%84%B8%EA%B8%88/" not in sm
        assert f"{SITE}/post/b-open/" in sm and "<lastmod>2026-10-01</lastmod>" in sm   # lastmod = updated

        # SEO: canonical 자기 자신, Article JSON-LD(날짜 포함), FAQPage 마크업 없음, 설명에 마감일
        p = read(out, "post/b-open/index.html")
        assert f'<link rel="canonical" href="{SITE}/post/b-open/">' in p
        ld = json.loads(re.search(r'<script type="application/ld\+json">(.*?)</script>', p, re.S).group(1))
        assert ld["@type"] == "Article" and ld["datePublished"] == "2026-10-01" and ld["dateModified"] == "2026-10-01"
        assert "신청 마감일: 2026년 12월 31일" in re.search(r'<meta name="description" content="(.*?)">', p).group(1)
        for f in out.rglob("*.html"):
            assert "FAQPage" not in f.read_text(encoding="utf-8")

        # 내부 링크: 같은 지역·분야 글 최대 3개, 자기 자신 제외
        rel = p[p.index("같은 지역·분야 글"):]
        assert rel.count('class="row"') <= 3 and "/post/b-open/" not in rel

        # 서식 행·목록 렌더링, 광고 자리는 꺼져 있으면 없다
        assert p.count('<section class="field') >= 6 and "<ul>" in p and "adsbygoogle" not in p
        # robots / 헤더(*.pages.dev 색인 차단) / 404
        assert "Sitemap: https://site.test/sitemap.xml" in read(out, "robots.txt")
        assert "https://:project.pages.dev/*\n  X-Robots-Tag: noindex" in read(out, "_headers")
        assert (out / "404.html").exists() and (out / "style.css").exists()
        # 소개 페이지: 연락처 미설정이면 안내 문구, 제목 앵커
        about = read(out, "about/index.html")
        assert "연락처 준비 중" in about and 'id="정정-요청"' in about


def test_ads_slots():
    cfg = yaml.safe_load(yaml.safe_dump(CFG))
    cfg["ads"] = {"enabled": True, "adsense_client": "ca-pub-1", "slots": {"mid": "11", "bottom": "22", "list": "33"}}
    with tempfile.TemporaryDirectory() as t:
        out, _ = make_site(Path(t), {"x-one": md_post("x-one", "광고 시험 (2026)")}, cfg=cfg)
        p = read(out, "post/x-one/index.html")
        assert p.count("adsbygoogle.js?client=ca-pub-1") == 1 and 'data-ad-slot="11"' in p and 'data-ad-slot="22"' in p
        mid = p.index('data-ad-slot="11"')           # 본문 중간 = 3번째 목차('얼마나 받나') 뒤, 4번째 앞
        assert p.index("얼마나 받나") < mid < p.index("언제까지 신청하나")
        assert p.index('data-ad-slot="22"') > p.index("참고 기사")  # 하단 1곳
        assert 'data-ad-slot="33"' in read(out, "index.html")      # 목록 페이지 하단


def test_guides_and_errors():
    with tempfile.TemporaryDirectory() as t:
        guide = "---\ntitle: 신청 전 확인할 것\nslug: before-apply\ndescription: 안내\nupdated: 2026-11-02\n---\n\n## 소득 기준\n\n본문\n"
        out, stats = make_site(Path(t), {"x-one": md_post("x-one", "가이드 시험 (2026)")}, guides={"before-apply": guide})
        assert stats["guides"] == 1 and (out / "guide/before-apply/index.html").exists() and (out / "guide/index.html").exists()
        assert "/guide/before-apply/" in read(out, "sitemap.xml") and "가이드" in read(out, "index.html")
    # 프론트매터 없음·슬러그 형식 오류·주소 중복은 빌드를 멈춘다(검수 단계에서 잡히도록)
    for bad in ({"x": "본문만 있다"}, {"x": md_post("Bad_Slug", "t")},):
        with tempfile.TemporaryDirectory() as t:
            try:
                make_site(Path(t), bad)
            except ValueError:
                continue
            raise AssertionError("잘못된 글이 빌드를 통과했다")
    with tempfile.TemporaryDirectory() as t:
        try:
            make_site(Path(t), {"one": md_post("same", "a"), "two": md_post("same", "b")})
        except ValueError:
            pass
        else:
            raise AssertionError("주소가 겹치는데 통과했다")


def test_site_url_and_sitemap_split():
    r = build.resolve_site_url
    assert r("https://example.invalid", {"CF_PAGES_URL": "https://abc.pages.dev/"}) == "https://abc.pages.dev"
    assert r("https://real.kr/", {"CF_PAGES_URL": "https://abc.pages.dev"}) == "https://real.kr"      # 실제 도메인이면 config 가 우선
    assert r("https://example.invalid", {"SITE_URL": "https://x.kr"}) == "https://x.kr"
    files = build.sitemap_files([(f"https://s/{i}", "") for i in range(12)], "https://s", 5)   # 5천 개 초과 시 분할
    assert sorted(files) == ["sitemap-1.xml", "sitemap-2.xml", "sitemap-3.xml", "sitemap.xml"] and "<sitemapindex" in files["sitemap.xml"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
