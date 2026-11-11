#!/usr/bin/env python3
"""정적 사이트 빌드 (지시서 §4, §5): content/ 마크다운 → dist/ HTML.

Cloudflare Pages 에서 `python scripts/build.py` 로 돌린다. 설치되는 의존성은 requirements.txt(사이트 전용)뿐이므로
requests·bs4 가 필요한 파이프라인 모듈(common.py 등)은 가져오지 않는다.

  content/posts/*.md    발행된 글 (PR 병합 = 발행)
  content/guide/*.md    직접 쓴 가이드
  content/pages/*.md    소개·개인정보처리방침
  → dist/               index, /post/, /region/, /topic/, /deadline/, /guide/, /about/, /privacy/,
                        sitemap.xml, robots.txt, _headers, 404.html

마감 상태는 빌드 시점의 오늘 날짜(KST)로 매번 다시 계산한다. 삭제하거나 noindex 처리하지 않는다.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

import markdown
import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parent.parent
KST = timezone(timedelta(hours=9))
SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
FM_RE = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.S)
TAX_LIKE = ("세금", "요금")   # 신청 절차가 없는 분야: 상태 문구를 '적용 종료'로 쓴다


@dataclass
class Page:
    kind: str                     # post | guide | page
    slug: str
    title: str
    description: str
    body_md: str
    region: str = ""
    topic: str = ""
    deadline: date | None = None
    published: date | None = None
    updated: date | None = None
    summary: str = ""
    html: str = ""
    url: str = ""                 # 경로(퍼센트 인코딩 전). 예: /post/fuel-tax/
    status: dict = field(default_factory=dict)


# ---------------------------------------------------------------- 읽기
def _date(v) -> date | None:
    if v in (None, ""):
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return datetime.strptime(str(v), "%Y-%m-%d").date()


def extract_summary(md: str) -> str:
    m = re.search(r"##\s*한 줄 요약\s*\n+(.*?)(?=\n##\s|\Z)", md, re.S)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""


def read_page(path: Path, kind: str) -> Page:
    m = FM_RE.match(path.read_text(encoding="utf-8"))
    if not m:
        raise ValueError(f"{path}: 프론트매터(---)가 없다")
    fm = yaml.safe_load(m.group(1)) or {}
    body = m.group(2).strip()
    slug = str(fm.get("slug") or path.stem)
    if not SLUG_RE.match(slug):
        raise ValueError(f"{path}: slug '{slug}' 형식이 올바르지 않다")
    if not fm.get("title"):
        raise ValueError(f"{path}: title 이 없다")
    p = Page(kind=kind, slug=slug, title=str(fm["title"]), description=str(fm.get("description", "")),
             body_md=body, region=str(fm.get("region") or ""), topic=str(fm.get("topic") or ""),
             deadline=_date(fm.get("deadline")), published=_date(fm.get("published")),
             updated=_date(fm.get("updated")) or _date(fm.get("published")))
    p.summary = extract_summary(body) if kind == "post" else p.description
    p.url = {"post": f"/post/{slug}/", "guide": f"/guide/{slug}/", "page": f"/{slug}/"}[kind]
    return p


def load_content(content_dir: Path) -> list[Page]:
    pages: list[Page] = []
    for sub, kind in (("posts", "post"), ("guide", "guide"), ("pages", "page")):
        d = content_dir / sub
        if d.exists():
            pages += [read_page(f, kind) for f in sorted(d.glob("*.md"))]
    seen: dict[str, str] = {}
    for p in pages:
        if p.url in seen:
            raise ValueError(f"주소가 겹친다: {p.url}")
        seen[p.url] = p.slug
    return pages


# ---------------------------------------------------------------- 마감 상태
def deadline_status(deadline: date | None, today: date, topic: str, soon_days: int = 7) -> dict:
    """state: closed | soon | open | none. 세금·요금은 '신청 마감' 대신 '적용 종료'로 표기한다."""
    end = "적용 종료" if topic in TAX_LIKE else "신청 마감"
    if deadline is None:
        return {"state": "none", "label": "마감일 미정", "days": None}
    days = (deadline - today).days
    if days < 0:
        return {"state": "closed", "label": end, "days": days}
    if days == 0:
        return {"state": "soon", "label": "오늘 " + ("종료" if topic in TAX_LIKE else "마감"), "days": 0}
    return {"state": "soon" if days <= soon_days else "open", "label": f"D-{days}", "days": days}


def by_latest(pages: list[Page]) -> list[Page]:
    """최신순. 마감된 글은 하단으로 내린다."""
    return sorted(pages, key=lambda p: (p.status["state"] == "closed", -(p.published or date.min).toordinal(), p.slug))


def by_deadline(pages: list[Page]) -> tuple[list[Page], list[Page]]:
    """(마감 가까운 순의 진행 중 글, 마감된 글 최근 마감 순). 마감일 없는 글은 제외한다."""
    dated = [p for p in pages if p.deadline]
    upcoming = sorted([p for p in dated if p.status["state"] != "closed"], key=lambda p: (p.deadline, p.slug))
    closed = sorted([p for p in dated if p.status["state"] == "closed"], key=lambda p: (-p.deadline.toordinal(), p.slug))
    return upcoming, closed


def related_posts(post: Page, posts: list[Page], n: int) -> list[Page]:
    """같은 분야·지역의 다른 글. 둘 다 같으면 먼저, 그다음 최신순."""
    cands = []
    for q in posts:
        if q.slug == post.slug:
            continue
        score = int(bool(post.topic) and q.topic == post.topic) * 2 + int(bool(post.region) and q.region == post.region)
        if score:
            cands.append((score, q))
    cands.sort(key=lambda t: (-t[0], t[1].status["state"] == "closed", -(t[1].published or date.min).toordinal(), t[1].slug))
    return [q for _, q in cands[:n]]


# ---------------------------------------------------------------- 마크다운 → HTML
_LIST_LINE = re.compile(r"^\s*([-*+]|\d+\.)\s+")


def fix_list_spacing(md: str) -> str:
    """목록 바로 앞 줄이 문단이면 빈 줄을 넣는다(Python-Markdown 은 빈 줄이 없으면 목록으로 읽지 않는다).
    이미 목록 안의 줄(연속 항목)이나 제목 뒤에는 넣지 않는다."""
    out: list[str] = []
    prev = ""
    for line in md.split("\n"):
        if _LIST_LINE.match(line) and prev.strip() and not _LIST_LINE.match(prev) and not prev.lstrip().startswith("#"):
            out.append("")
        out.append(line)
        prev = line
    return "\n".join(out)


def _slugify(value: str, sep: str) -> str:
    """제목 앵커(id). 기본 구현은 한글을 모두 지우므로 글자는 남긴다. '정정 요청' → '정정-요청'."""
    value = re.sub(r"[^\w\s-]", "", value, flags=re.U).strip().lower()
    return re.sub(r"[-\s]+", sep, value)


def render_md(md_text: str, heading_ids: bool = False) -> str:
    """원문의 raw HTML 은 이스케이프한다(검수를 거쳐도 스크립트가 섞이지 않게).

    heading_ids=True 면 제목에 앵커 id 를 붙인다(소개·가이드용). 글은 sectionize 가 `<h2>` 모양에 의존하므로 끈다."""
    safe = fix_list_spacing(md_text).replace("<", "&lt;").replace(">", "&gt;")
    return markdown.markdown(safe, extensions=["sane_lists"] + (["toc"] if heading_ids else []),
                             extension_configs={"toc": {"slugify": _slugify}}, output_format="html")


def sectionize(body_html: str, ad_mid: str = "") -> str:
    """h2 마다 '라벨 | 값' 서식 행으로 감싼다. 한 줄 요약은 머리말로, 3번째 목차 뒤에 광고 자리를 둔다(§7)."""
    parts = re.split(r"(?=<h2>)", body_html)
    out = [parts[0]] if parts[0].strip() else []
    for i, s in enumerate(parts[1:], 1):
        m = re.match(r"<h2>(.*?)</h2>(.*)", s, re.S)
        if not m:
            out.append(s)
            continue
        title, rest = m.group(1), m.group(2)
        if title == "한 줄 요약":
            out.append(f'<div class="lead"><h2 class="sr-only">{title}</h2>{rest}</div>')
        else:
            cls = "field refs" if title == "참고 기사" else "field"
            out.append(f'<section class="{cls}"><h2>{title}</h2><div class="field-body">{rest}</div></section>')
        if ad_mid and i == 3:
            out.append(ad_mid)
    return "\n".join(out)


# ---------------------------------------------------------------- 사이트 주소
def resolve_site_url(cfg_url: str, environ: dict) -> str:
    """SITE_URL 환경변수 > (도메인이 placeholder 면) Cloudflare 가 주는 CF_PAGES_URL > config 값."""
    if environ.get("SITE_URL"):
        return environ["SITE_URL"].rstrip("/")
    host = (urlparse(cfg_url).hostname or "").lower()
    if (host.endswith(".invalid") or host in ("example.com", "example.org", "example.net")) and environ.get("CF_PAGES_URL"):
        return environ["CF_PAGES_URL"].rstrip("/")
    return cfg_url.rstrip("/")


def kdate(d: date | None) -> str:
    return f"{d.year}년 {d.month}월 {d.day}일" if d else ""


def urlq(path: str) -> str:
    return quote(path, safe="/")


# ---------------------------------------------------------------- 사이트맵·robots
def sitemap_files(entries: list[tuple[str, str]], site_url: str, chunk: int) -> dict[str, str]:
    def urlset(rows):
        body = "".join(f"<url><loc>{html.escape(loc)}</loc>" + (f"<lastmod>{lm}</lastmod>" if lm else "") + "</url>" for loc, lm in rows)
        return f'<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{body}</urlset>\n'

    if len(entries) <= chunk:
        return {"sitemap.xml": urlset(entries)}
    files, idx = {}, []
    for i in range(0, len(entries), chunk):
        name = f"sitemap-{i // chunk + 1}.xml"
        files[name] = urlset(entries[i:i + chunk])
        idx.append(f"<sitemap><loc>{site_url}/{name}</loc></sitemap>")
    files["sitemap.xml"] = ('<?xml version="1.0" encoding="UTF-8"?>\n<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                            + "".join(idx) + "</sitemapindex>\n")
    return files


# ---------------------------------------------------------------- 빌드
def build(cfg: dict, content_dir: Path, out_dir: Path, today: date, site_url: str,
          templates_dir: Path | None = None, static_dir: Path | None = None) -> dict:
    templates_dir = templates_dir or ROOT / "templates"
    static_dir = static_dir or ROOT / "static"
    bc, site = cfg["build"], cfg["site"]
    ads = cfg.get("ads", {})

    env = Environment(loader=FileSystemLoader(str(templates_dir)), autoescape=select_autoescape(["html"]),
                      trim_blocks=True, lstrip_blocks=True)
    env.filters["kdate"] = kdate
    env.filters["urlq"] = urlq
    env.filters["jsonld"] = lambda o: json.dumps(o, ensure_ascii=False).replace("</", "<\\/")

    pages = load_content(content_dir)
    posts = [p for p in pages if p.kind == "post"]
    guides = [p for p in pages if p.kind == "guide"]
    statics = [p for p in pages if p.kind == "page"]
    for p in pages:
        p.status = deadline_status(p.deadline, today, p.topic, bc["soon_days"]) if p.kind == "post" else {"state": "none", "label": "", "days": None}

    def ad(name: str) -> str:
        if not ads.get("enabled") or not ads.get("adsense_client"):
            return ""
        slot = (ads.get("slots") or {}).get(name, "")
        return (f'<div class="ad-slot ad-{name}"><ins class="adsbygoogle" style="display:block" '
                f'data-ad-client="{html.escape(ads["adsense_client"])}" data-ad-slot="{html.escape(slot)}" '
                f'data-ad-format="auto" data-full-width-responsive="true"></ins>'
                f'<script>(adsbygoogle = window.adsbygoogle || []).push({{}});</script></div>')

    # 지역·분야 묶음. 글이 thin_list_min 미만이면 noindex + 사이트맵 제외
    groups: dict[str, dict[str, list[Page]]] = {"region": {}, "topic": {}}
    for p in posts:
        if p.region:
            groups["region"].setdefault(p.region, []).append(p)
        if p.topic:
            groups["topic"].setdefault(p.topic, []).append(p)

    def nav_groups():
        return {kind: [{"name": k, "count": len(v), "url": f"/{kind}/{k}/"} for k, v in sorted(g.items())]
                for kind, g in groups.items()}

    common = {"site": site, "site_url": site_url, "today": today, "ad_list": ad("list"), "ad_bottom": ad("bottom"),
              "ads_enabled": bool(ads.get("enabled") and ads.get("adsense_client")), "adsense_client": ads.get("adsense_client", ""),
              "nav_groups": nav_groups(), "has_guides": bool(guides)}

    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    if static_dir.exists():
        for f in static_dir.iterdir():
            if f.is_file():
                shutil.copy(f, out_dir / f.name)

    def write(path: str, content: str) -> None:
        dest = out_dir / path.lstrip("/")
        if path.endswith("/"):
            dest = dest / "index.html"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")

    def render(tpl: str, **kw) -> str:
        return env.get_template(tpl).render(**common, **kw)

    sitemap: list[tuple[str, str]] = []

    def add_sitemap(url: str, lastmod: date | None = None) -> None:
        sitemap.append((site_url + urlq(url), lastmod.isoformat() if lastmod else ""))

    # 글
    for p in posts:
        p.html = sectionize(render_md(p.body_md), ad("mid"))
        rel = related_posts(p, posts, bc["related_max"])
        desc = p.summary + (f" 신청 마감일: {kdate(p.deadline)}." if p.deadline and p.topic not in TAX_LIKE else
                            f" 적용 종료일: {kdate(p.deadline)}." if p.deadline else "")
        desc = desc.strip()
        ld = {"@context": "https://schema.org", "@type": "Article", "headline": p.title, "description": desc,
              "datePublished": (p.published or today).isoformat(), "dateModified": (p.updated or p.published or today).isoformat(),
              "mainEntityOfPage": {"@type": "WebPage", "@id": site_url + urlq(p.url)},
              "author": {"@type": "Organization", "name": site["name"]},
              "publisher": {"@type": "Organization", "name": site["name"]}}
        write(p.url, render("post.html", page=p, description=desc, canonical=site_url + urlq(p.url), related=rel, jsonld=ld,
                            region_url=f"/region/{p.region}/" if p.region else "", topic_url=f"/topic/{p.topic}/" if p.topic else ""))
        add_sitemap(p.url, p.updated)

    # 가이드·소개·정책
    for p in guides + statics:
        body = p.body_md.replace("{{ site_name }}", site["name"]).replace("{{ contact }}", site.get("contact_email") or "연락처 준비 중")
        p.html = render_md(body, heading_ids=True)
        write(p.url, render("page.html", page=p, description=p.description, canonical=site_url + urlq(p.url)))
        add_sitemap(p.url, p.updated)
    if guides:
        write("/guide/", render("list.html", heading="가이드", intro="지원금을 신청하기 전에 알아 두면 좋은 내용을 정리했습니다.",
                                items=sorted(guides, key=lambda g: g.title), canonical=site_url + "/guide/", noindex=False, show_status=False,
                                description="지원금 신청 전 확인사항과 용어 설명"))
        add_sitemap("/guide/")

    # 지역·분야 페이지
    for kind, label in (("region", "지역"), ("topic", "분야")):
        for name, items in groups[kind].items():
            thin = len(items) < bc["thin_list_min"]
            url = f"/{kind}/{name}/"
            heading = f"{name} 지원금·제도 소식" if kind == "region" else f"{name} 소식"
            write(url, render("list.html", heading=heading, intro=f"{name} {label}에 해당하는 글입니다.", items=by_latest(items),
                              canonical=site_url + urlq(url), noindex=thin, show_status=True,
                              description=f"{name} 지원금·제도 변경 소식 {len(items)}건"))
            if not thin:
                add_sitemap(url)

    # 마감 임박
    upcoming, closed = by_deadline(posts)
    write("/deadline/", render("deadline.html", upcoming=upcoming, closed=closed, canonical=site_url + "/deadline/",
                               description="신청 마감이 가까운 지원금을 마감일 순으로 모았습니다"))
    add_sitemap("/deadline/")

    # 메인
    latest = by_latest(posts)
    write("/", render("index.html", latest=latest[:10], upcoming=upcoming[:6], total=len(posts), canonical=site_url + "/",
                      description=site["tagline"]))
    add_sitemap("/")

    # 404, robots, headers, sitemap
    write("404.html", render("404.html", canonical=""))
    for name, content in sitemap_files(sitemap, site_url, bc["sitemap_chunk"]).items():
        write(name, content)
    write("robots.txt", f"User-agent: *\nAllow: /\n\nSitemap: {site_url}/sitemap.xml\n")
    # 초안 미리보기·기본 도메인(*.pages.dev)은 색인을 막는다. robots.txt 는 호스트를 가리지 못하므로 헤더로 막는다(§5).
    write("_headers", "https://:project.pages.dev/*\n  X-Robots-Tag: noindex\n\n/*\n  X-Content-Type-Options: nosniff\n"
                      "  Referrer-Policy: strict-origin-when-cross-origin\n")

    return {"posts": len(posts), "guides": len(guides), "pages": len(statics), "sitemap_urls": len(sitemap),
            "closed_posts": sum(1 for p in posts if p.status["state"] == "closed"),
            "thin_lists": sum(1 for g in groups.values() for v in g.values() if len(v) < bc["thin_list_min"])}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="dist")
    ap.add_argument("--content", default="content")
    ap.add_argument("--today", help="YYYY-MM-DD (기본: 오늘, KST)")
    ap.add_argument("--site-url", help="기본: SITE_URL > CF_PAGES_URL(도메인 placeholder 일 때) > config")
    args = ap.parse_args()

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    today = _date(args.today) if args.today else datetime.now(KST).date()
    site_url = (args.site_url or resolve_site_url(cfg["site"]["url"], dict(os.environ))).rstrip("/")
    stats = build(cfg, ROOT / args.content, ROOT / args.out, today, site_url)
    print(json.dumps({"today": today.isoformat(), "site_url": site_url, **stats}, ensure_ascii=False), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
