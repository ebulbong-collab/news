"""기사 본문 가져오기 (지시서 2-4).

- 묶음에 든 기사만 가져온다. robots.txt 가 막은 경로는 가져오지 않고 RSS 요약으로 대신한다.
- 같은 호스트에는 1초에 1회 이하로 요청한다(common.Fetcher). UA 는 common.body_user_agent.
- 로그인·유료로 보이는 페이지(본문이 너무 짧음)는 건너뛰고 RSS 요약으로 대신한다.
- 본문은 메모리에서만 다룬다. 디스크에 쓰지 않고 이미지는 받지 않는다.
  (사실 추출과 유사도 검사가 끝나면 호출한 쪽이 참조를 버린다.)
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

import feedparser
import trafilatura

from common import Fetcher, RobotsCache, body_user_agent, clean_text, load_yaml, norm_link

BOILERPLATE = re.compile(r"(무단\s*전재|저작권자|ⓒ|©|Copyright|재배포\s*금지|AI\s*학습)", re.I)
RSS_MIN_CHARS = 50


@dataclass
class Article:
    publisher: str
    title: str
    link: str
    pub: str | None
    source: str = "none"   # body | rss_summary | none
    text: str = ""
    robots: str = ""       # allowed | blocked | unknown
    note: str = ""         # 대체·실패 사유

    def info(self) -> dict:
        """보고서용 요약. 본문 텍스트는 넣지 않는다."""
        return {"publisher": self.publisher, "link": self.link, "source": self.source,
                "robots": self.robots, "chars": len(self.text), "note": self.note}

    def date(self) -> str:
        return (self.pub or "")[:10]


def pick_articles(articles: list[dict], max_n: int) -> list[dict]:
    """묶음당 최대 max_n 건. 서로 다른 언론사를 먼저 고르고 남는 자리를 채운다."""
    rows = sorted(articles, key=lambda a: a["pub"] or "", reverse=True)
    chosen, seen_pub = [], set()
    for a in rows:
        if a["publisher"] not in seen_pub and len(chosen) < max_n:
            chosen.append(a)
            seen_pub.add(a["publisher"])
    for a in rows:
        if len(chosen) >= max_n:
            break
        if a not in chosen:
            chosen.append(a)
    return chosen


def extract_fusion(html: str) -> str:
    """Arc XP 계열 사이트(조선일보 등)는 본문 문단을 HTML 안의 `Fusion.globalContent` JSON 에 담는다."""
    key = "Fusion.globalContent="
    i = html.find(key)
    if i < 0:
        return ""
    try:
        data, _ = json.JSONDecoder().raw_decode(html[i + len(key):])
    except ValueError:
        return ""
    paras = [clean_text(el.get("content", "")) for el in data.get("content_elements", [])
             if isinstance(el, dict) and el.get("type") == "text"]
    return "\n".join(p for p in paras if p)


def extract_body(content: bytes, max_chars: int, min_chars: int = 200) -> str:
    text = trafilatura.extract(content, include_comments=False, include_tables=False,
                               favor_precision=True) or ""
    if len(text) < min_chars:  # 일반 추출이 실패하면 임베디드 JSON 을 시도한다
        text = extract_fusion(content.decode("utf-8", "ignore")) or text
    lines = [l.strip() for l in text.splitlines() if l.strip() and not BOILERPLATE.search(l)]
    return "\n".join(lines)[:max_chars]


def rss_index(publishers: set[str], fetcher: Fetcher, max_chars: int) -> dict[str, str]:
    """RSS 요약 대체용: 해당 언론사 피드에서 link → 요약·본문 텍스트."""
    idx: dict[str, str] = {}
    for f in load_yaml("feeds.yaml")["feeds"]:
        if not f["enabled"] or f["publisher"] not in publishers:
            continue
        r, _ = fetcher.get(f["url"])
        if r is None or r.status_code != 200:
            continue
        for e in feedparser.parse(r.content).entries:
            cands = [clean_text(e.get("summary") or e.get("description") or "")]
            cands += [clean_text(c.get("value", "")) for c in e.get("content", [])]
            text = max(cands, key=len)
            link = norm_link(e.get("link", ""))
            if link and len(text) > len(idx.get(link, "")):
                idx[link] = text[:max_chars]
    return idx


def fetch_articles(arts: list[dict], cfg: dict) -> list[Article]:
    fetcher = Fetcher(cfg, body_user_agent(cfg))
    robots = RobotsCache(fetcher, cfg["http"]["bot_token"])
    max_chars = cfg["limits"]["max_body_chars"]
    min_chars = cfg["draft"]["body_min_chars"]

    out: list[Article] = []
    for a in arts:
        art = Article(a["publisher"], a["title"], a["link"], a.get("pub") or a.get("first_seen"))
        art.robots, _ = robots.check(art.link)
        if art.robots == "allowed":
            r, err = fetcher.get(art.link)
            if r is None:
                art.note = f"요청 실패({err})"
            elif r.status_code != 200:
                art.note = f"HTTP {r.status_code}"
            else:
                text = extract_body(r.content, max_chars, min_chars)
                if len(text) >= min_chars:
                    art.source, art.text = "body", text
                    if not art.pub:  # 피드에 날짜가 없는 언론사(한겨레 등)는 페이지 메타데이터에서 보충
                        meta = trafilatura.extract_metadata(r.content)
                        art.pub = (meta.date if meta and meta.date else None)
                else:
                    art.note = f"본문 추출 {len(text)}자(기준 {min_chars}자 미만)"
        else:
            art.note = f"robots {art.robots}"
        out.append(art)

    need = [a for a in out if a.source != "body"]
    if need:
        idx = rss_index({a.publisher for a in need}, fetcher, max_chars)
        for art in need:
            text = idx.get(art.link, "")
            if len(text) >= RSS_MIN_CHARS:
                art.source, art.text = "rss_summary", text
                art.note = (art.note + " → RSS 요약으로 대체").strip()
            else:
                art.source = "none"
                art.note = (art.note + " → RSS 요약도 없음").strip()
    return out
