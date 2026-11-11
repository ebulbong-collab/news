#!/usr/bin/env python3
"""0단계 피드 검증 (업무지시서 6-4).

data/feed_specs.csv 의 경제·사회·전체 피드를 실제로 호출해 아래를 점검한다.
  - 응답 여부, 항목 수, 최신 항목 날짜 (7일 넘게 멈춘 피드는 제외)
  - description 길이 (요약인지 본문 전체인지)
  - 해당 언론사 robots.txt 의 기사 경로 허용 여부
  - 최근 7일 항목 중 포함 키워드에 걸리는 건수

산출물
  data/feed_check_result.json        피드별 원자료
  reports/stage0_feed_check.md       표 형태 보고서
  feeds.yaml                         사용 여부(enabled)가 채워진 피드 목록

통과 기준: 정상 피드 8개 이상(언론사 5곳 이상) + 키워드 통과 기사 하루 평균 20건 이상.
"""
from __future__ import annotations

import argparse
import calendar
import csv
import email.utils
import json
import re
import statistics
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urldefrag, urlparse
from urllib.robotparser import RobotFileParser

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
KST = timezone(timedelta(hours=9))
ROBOTS_SAMPLE_PER_FEED = 3


# ---------------------------------------------------------------- 공통 도구
class HostLimiter:
    """같은 호스트에는 1초에 1회 이하로만 요청하도록 직렬화한다."""

    def __init__(self, interval: float):
        self.interval = interval
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
        self._last: dict[str, float] = {}

    def get(self, session: requests.Session, url: str, **kw) -> requests.Response:
        host = urlparse(url).netloc
        with self._guard:
            lock = self._locks[host]
        with lock:
            wait = self.interval - (time.monotonic() - self._last.get(host, 0.0))
            if wait > 0:
                time.sleep(wait)
            try:
                return session.get(url, **kw)
            finally:
                self._last[host] = time.monotonic()


class Fetcher:
    def __init__(self, cfg: dict):
        h = cfg["http"]
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": h["user_agent_check"], "Accept": "*/*"})
        self.timeout = h["timeout_sec"]
        self.limiter = HostLimiter(h["min_interval_sec"])

    def get(self, url: str, retries: int = 1):
        """(response | None, error 문자열 | None). 연결 오류·5xx 는 1회 재시도."""
        last_err = None
        for attempt in range(retries + 1):
            try:
                r = self.limiter.get(self.session, url, timeout=self.timeout, allow_redirects=True)
                if r.status_code >= 500 and attempt < retries:
                    time.sleep(2)
                    continue
                return r, None
            except requests.exceptions.Timeout:
                last_err = "timeout"
            except requests.exceptions.SSLError as e:
                last_err = f"ssl: {str(e)[:80]}"
            except requests.exceptions.RequestException as e:
                last_err = f"{type(e).__name__}: {str(e)[:80]}"
            if attempt < retries:
                time.sleep(2)
        return None, last_err


class RobotsCache:
    """origin 별 robots.txt 를 한 번만 받아 판정한다."""

    def __init__(self, fetcher: Fetcher, bot_token: str):
        self.f = fetcher
        self.token = bot_token
        self._lock = threading.Lock()
        self._cache: dict[str, dict] = {}

    def _load(self, origin: str) -> dict:
        r, err = self.f.get(origin + "/robots.txt", retries=0)
        if r is None:
            return {"status": f"error({err})", "rp": None, "mode": "unknown"}
        code = r.status_code
        if code == 200:
            rp = RobotFileParser()
            rp.parse(r.text.splitlines())
            return {"status": "200", "rp": rp, "mode": "rules",
                    "crawl_delay": rp.crawl_delay(self.token)}
        if code in (401, 403):  # 표준 라이브러리와 같이 보수적으로 전부 금지로 본다
            return {"status": str(code), "rp": None, "mode": "deny_all"}
        if 400 <= code < 500:   # robots.txt 없음 → 제한 없음
            return {"status": str(code), "rp": None, "mode": "allow_all"}
        return {"status": str(code), "rp": None, "mode": "unknown"}

    def check(self, url: str) -> tuple[str, str]:
        """(판정, robots 상태). 판정: allowed | blocked | unknown"""
        p = urlparse(url)
        origin = f"{p.scheme}://{p.netloc}"
        with self._lock:
            if origin not in self._cache:
                self._cache[origin] = self._load(origin)
            info = self._cache[origin]
        mode = info["mode"]
        if mode == "allow_all":
            return "allowed", info["status"]
        if mode == "deny_all":
            return "blocked", info["status"]
        if mode == "unknown":
            return "unknown", info["status"]
        ok = info["rp"].can_fetch(self.token, url)
        return ("allowed" if ok else "blocked"), info["status"]


def clean_text(html: str) -> str:
    return BeautifulSoup(html or "", "html.parser").get_text(" ", strip=True)


TZ_RE = re.compile(r"(Z|GMT|UTC|UT|[+-]\d{2}:?\d{2}|\b[A-Z]{3,4})\s*$")


def entry_dt(e) -> datetime | None:
    """항목 발행 시각(UTC). 타임존 표기가 없는 날짜는 한국 시각(KST)으로 본다.

    - feedparser 는 타임존 없는 날짜를 UTC 로 읽어 9시간 어긋나므로 되돌린다.
    - 'Wed,11 Nov 2026 …' 처럼 쉼표 뒤 공백이 없는 형식은 feedparser 가 못 읽어 직접 파싱한다.
    """
    raw = (e.get("published") or e.get("updated") or "").strip()
    t = e.get("published_parsed") or e.get("updated_parsed")
    if t:
        dt = datetime.fromtimestamp(calendar.timegm(t), tz=timezone.utc)
        if raw and not TZ_RE.search(raw):
            dt -= timedelta(hours=9)
        return dt
    if not raw:
        return None
    fixed = re.sub(r"^([A-Za-z]{3}),(\S)", r"\1, \2", raw)
    try:
        dt = email.utils.parsedate_to_datetime(fixed)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KST)
    return dt.astimezone(timezone.utc)


def norm_link(link: str) -> str:
    return urldefrag(link.strip())[0]


class Keywords:
    """0단계 판정은 지시서 원안(v1) 규칙 그대로다: strong+weak 를 제목에서 단순 매칭, 원안 exclude."""

    def __init__(self, path: Path | None = None):
        from common import Keywords as V2  # 1단계 정밀화 규칙과 같은 keywords.yaml 을 읽는다
        kw = V2()
        self.include = kw.strong + kw.weak
        self.exclude = kw.exclude_v1

    def match(self, title: str) -> list[str]:
        """통과하면 걸린 포함 키워드 목록, 아니면 빈 리스트."""
        if any(k in title for k in self.exclude):
            return []
        return [k for k in self.include if k in title]


# 지시서 포함 키워드 중 '지원금성' 핵심어. 통과 건수 중 노이즈 비율을 가늠하는 보조 지표용.
CORE_KEYWORDS = ["지원금", "보조금", "바우처", "환급", "수당", "장려금", "감면"]
# 키워드 목록 확장 여부를 판단하기 위한 참고용 용어(통과 판정에는 쓰지 않는다).
PROBE_TERMS = ["지원", "쿠폰", "혜택", "연금", "공제", "요금", "신청", "급여", "무상", "할인"]


# ---------------------------------------------------------------- 피드 1개 점검
def short_err(err: str | None) -> str:
    err = err or ""
    if "NameResolution" in err or "Failed to resolve" in err or "Name or service" in err:
        return "DNS 해석 실패"
    if "ProxyError" in err or "Max retries" in err:
        return "연결 실패(프록시/서버 무응답)"
    if "timeout" in err:
        return "시간 초과"
    return err[:50]


def check_feed(spec: dict, cfg: dict, fetcher: Fetcher, robots: RobotsCache,
               kw: Keywords, now: datetime) -> dict:
    fc = cfg["feed_check"]
    res = {
        "publisher": spec["publisher"], "title": spec["title"],
        "category": spec["categories"], "url": spec["url"], "url_used": spec["url"],
        "http": None, "status": None, "reason": "",
        "n_items": 0, "n_dated": 0, "newest": None, "oldest": None,
        "newest_age_h": None, "span_days": None, "future_items": 0,
        "channel_updated": None,
        "desc_median": None, "desc_p90": None, "has_full_content": 0,
        "robots": {"allowed": 0, "blocked": 0, "unknown": 0, "sampled": 0, "status": None},
        "recent": [],  # 최근 window_days 일 항목 {link,title,pub,kw,core,desc_core}
    }
    r, err = fetcher.get(spec["url"])
    if r is None and spec["url"].startswith("http://"):
        # 이 환경은 평문 HTTP 가 막혀 있을 수 있어 https 로도 시도한다.
        alt = "https://" + spec["url"][len("http://"):]
        r2, err2 = fetcher.get(alt, retries=0)
        if r2 is not None and r2.status_code == 200:
            r, err = r2, None
            res["url_used"] = alt
        else:
            err = f"{err} / https 재시도도 실패({short_err(err2)})"
    if r is None:
        res.update(status="FAIL", reason=short_err(err))
        return res
    res["http"] = r.status_code
    if r.status_code != 200:
        res.update(status="FAIL", reason=f"HTTP {r.status_code}")
        return res

    d = feedparser.parse(r.content)
    entries = d.entries
    res["n_items"] = len(entries)
    if not entries:
        why = "항목 0개"
        if d.bozo:
            why += f" (파싱 경고: {type(d.bozo_exception).__name__})"
        res.update(status="FAIL", reason=why)
        return res

    # robots.txt: 이 피드 기사 링크 일부를 표본으로 판정 (날짜 문제와 무관하게 수행)
    links = [norm_link(e.get("link", "")) for e in entries if e.get("link")]
    step = max(1, len(links) // ROBOTS_SAMPLE_PER_FEED)
    for link in links[::step][:ROBOTS_SAMPLE_PER_FEED]:
        verdict, st = robots.check(link)
        res["robots"][verdict] += 1
        res["robots"]["sampled"] += 1
        res["robots"]["status"] = st

    lens, full = [], 0
    for e in entries:
        summ = e.get("summary") or e.get("description") or ""
        lens.append(len(clean_text(summ)))
        if e.get("content"):
            full += 1
    res["desc_median"] = int(statistics.median(lens))
    res["desc_p90"] = int(sorted(lens)[int(0.9 * (len(lens) - 1))])
    res["has_full_content"] = full

    dts = [entry_dt(e) for e in entries]
    dated = [x for x in dts if x]
    res["n_dated"] = len(dated)

    chan = d.feed.get("updated_parsed")
    if chan:
        res["channel_updated"] = datetime.fromtimestamp(
            calendar.timegm(chan), tz=timezone.utc).astimezone(KST).strftime("%Y-%m-%d %H:%M")

    window_start = now - timedelta(days=fc["window_days"])
    if dated:
        newest, oldest = max(dated), min(dated)
        res["newest"] = newest.astimezone(KST).strftime("%Y-%m-%d %H:%M")
        res["oldest"] = oldest.astimezone(KST).strftime("%Y-%m-%d %H:%M")
        res["newest_age_h"] = round((now - newest).total_seconds() / 3600, 1)
        res["span_days"] = round(min(fc["window_days"], (now - oldest).total_seconds() / 86400), 2)
        res["future_items"] = sum(1 for x in dated if x > now + timedelta(hours=1))

    # 최근 window 안의 항목과 키워드 통과 여부
    # (날짜 없는 피드는 '피드가 보여주는 최신 항목 전체'를 대신 담아 별도 집계한다)
    for e, dt in zip(entries, dts):
        if dated and (not dt or dt < window_start or dt > now + timedelta(days=1)):
            continue
        title = clean_text(e.get("title", ""))
        desc = clean_text(e.get("summary") or e.get("description") or "")
        hit = kw.match(title)
        res["recent"].append({
            "link": norm_link(e.get("link", "")), "title": title,
            "pub": dt.astimezone(KST).strftime("%Y-%m-%d %H:%M") if dt else None,
            "kw": hit, "core": [k for k in hit if k in CORE_KEYWORDS],
            "desc_core": [k for k in CORE_KEYWORDS if k in desc and k not in title],
            "probe": [k for k in PROBE_TERMS if k in title],
        })

    if not dated:
        # 항목에 날짜가 없으면 채널 갱신 시각으로 최신성만 본다.
        if chan and (now - datetime.fromtimestamp(calendar.timegm(chan), tz=timezone.utc)
                     ) <= timedelta(days=fc["stale_days"]):
            res.update(status="NODATE", reason="항목에 날짜 없음(채널 갱신시각만 있음)")
        else:
            res.update(status="FAIL", reason="날짜 정보 없음")
    elif res["newest_age_h"] > fc["stale_days"] * 24:
        res.update(status="STALE", reason=f"최신 항목이 {res['newest_age_h'] / 24:.0f}일 전")
    else:
        res["status"] = "OK"
    return res


# ---------------------------------------------------------------- 보고서
def fmt_robots(rb: dict) -> str:
    if not rb["sampled"]:
        return "-"
    if rb["allowed"] == rb["sampled"]:
        return f"허용({rb['status']})"
    if rb["blocked"] == rb["sampled"]:
        return f"금지({rb['status']})"
    if rb["unknown"] == rb["sampled"]:
        return f"불명({rb['status']})"
    return f"혼합 허용 {rb['allowed']}/{rb['sampled']}"


def desc_kind(median: int | None, full: int, n: int) -> str:
    if median is None:
        return "-"
    if full and full >= n / 2:
        return "본문 포함(content)"
    if median < 300:
        return "요약"
    if median < 800:
        return "긴 요약"
    return "본문 전체 가능성"


STATUS_LABEL = {"OK": "OK", "NODATE": "**날짜없음**", "STALE": "**정지**", "FAIL": "**실패**"}


def build_report(results: list[dict], cfg: dict, now: datetime, S: dict) -> str:
    fc = cfg["feed_check"]
    L = []
    L.append("# 0단계 피드 검증 결과")
    L.append("")
    L.append(f"- 실행: {now.astimezone(KST):%Y-%m-%d %H:%M} KST (스냅샷 1회)")
    L.append(f"- 대상: `feed_specs.csv` 중 {', '.join(fc['categories'])} → "
             f"{S['n_feeds']}개 피드 / {S['n_publishers']}개 언론사")
    L.append(f"- 요청 UA: `{cfg['http']['user_agent_check']}`, 같은 호스트 {cfg['http']['min_interval_sec']}초 간격")
    L.append("- 실행 환경: Claude Code 클라우드 샌드박스(프록시 경유). 운영 환경(GitHub Actions)과 "
             "출구 IP·네트워크가 달라 일부 언론사는 결과가 다를 수 있다.")
    L.append("")
    L.append("## 판정")
    L.append("")
    L.append("| 기준 | 요구 | 실측 | 결과 |")
    L.append("|---|---|---|---|")
    for name, need, got, ok in S["criteria"]:
        L.append(f"| {name} | {need} | {got} | {'통과' if ok else '**미달**'} |")
    L.append("")
    L.append(f"**종합(지시서 기준 그대로): {'통과' if S['passed'] else '미달 — 중단하고 보고'}**")
    L.append("")
    L.append("## 키워드 통과 건수 — 측정 방식별")
    L.append("")
    L.append("피드는 대부분 최근 수 시간~1일치 최신 항목만 담고 있어 '7일 합계 ÷ 7'은 실제보다 작게 나온다. "
             "그래서 수집 범위로 보정한 추정치를 함께 적는다. 둘 다 스냅샷 1회 기준이라 오차가 크다.")
    L.append("")
    L.append("| 집계 | 건수(스냅샷) | 하루 평균: 지시서 방식(÷7) | 하루 평균: 수집범위 보정 추정 |")
    L.append("|---|---|---|---|")
    L.append(f"| 포함 키워드 전체 통과(제목) | {S['n_pass']} | {S['per_day']:.1f} | {S['adj_pass']:.1f} |")
    L.append(f"| 핵심어(지원금·보조금·바우처·환급·수당·장려금·감면) 제목 | {S['n_core']} | "
             f"{S['n_core'] / fc['window_days']:.1f} | {S['adj_core']:.1f} |")
    L.append(f"| 핵심어가 제목 또는 요약에 있음 | {S['n_core_any']} | "
             f"{S['n_core_any'] / fc['window_days']:.1f} | {S['adj_core_any']:.1f} |")
    L.append("")
    L.append(f"- 집계 대상: 정상 피드의 링크 중복 제거 후 {S['n_unique']}건 "
             f"(날짜 없는 피드 제외). 보정 방식: 언론사별로 수집 범위(가장 긴 피드 기준, 최대 {fc['window_days']}일)로 나눠 합산.")
    L.append("- 통과 건수의 키워드 빈도: " + ", ".join(f"{k} {v}" for k, v in S["kw_counter"].most_common()))
    L.append(f"- 통과 기사가 나온 언론사: {S['pass_publishers']}곳 / 핵심어 기사가 나온 언론사: {S['core_publishers']}곳")
    L.append("- 참고(판정 미사용) 제목 용어 빈도: " + ", ".join(f"{k} {v}" for k, v in S["probe_counter"].most_common()))
    if S["nodate_pass"] is not None:
        L.append(f"- 날짜 없는 피드({S['nodate_feeds']})의 항목 {S['nodate_items']}건 중 포함 키워드 통과 {S['nodate_pass']}건 "
                 "(위 집계에서 제외)")
    L.append("")
    L.append("## 피드별 결과")
    L.append("")
    L.append("| 언론사 | 피드 | 상태 | 항목수 | 최신 항목(KST) | 수집범위(일) | desc 중앙값/P90(자) | 유형 | robots | 7일내 항목 | 키워드 통과 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for r in sorted(results, key=lambda x: (x["publisher"], x["category"])):
        st = STATUS_LABEL[r["status"]] + ("" if r["status"] == "OK" else f" {r['reason']}")
        dl = f"{r['desc_median']}/{r['desc_p90']}" if r["desc_median"] is not None else "-"
        kind = desc_kind(r["desc_median"], r["has_full_content"], r["n_items"])
        npass = sum(1 for x in r["recent"] if x["kw"])
        sd = f"{r['span_days']:.1f}" if r["span_days"] is not None else "-"
        newest = r["newest"] or (f"(채널 {r['channel_updated']})" if r["channel_updated"] else "-")
        https_note = " ※https로 전환" if r["url_used"] != r["url"] else ""
        L.append(f"| {r['publisher']} | {r['title']}({r['category']}){https_note} | {st} | {r['n_items']} | "
                 f"{newest} | {sd} | {dl} | {kind} | {fmt_robots(r['robots'])} | "
                 f"{len(r['recent'])} | {npass} |")
    L.append("")
    L.append("## 언론사별 요약")
    L.append("")
    L.append("| 언론사 | 정상 피드 | 7일 키워드 통과(중복 제거) | 핵심어(제목) | robots 표본 | 비고 |")
    L.append("|---|---|---|---|---|---|")
    for p, v in S["by_publisher"].items():
        L.append(f"| {p} | {v['healthy']}/{v['feeds']} | {v['pass']} | {v['core']} | {v['robots']} | {v['note']} |")
    L.append("")
    L.append("## 주의")
    L.append("")
    for n in S["notes"]:
        L.append(f"- {n}")
    L.append("")
    return "\n".join(L)


def write_feeds_yaml(results: list[dict], path: Path):
    lines = ["# RSS 피드 목록. enabled 는 0단계 검증 결과로 채웠다.",
             "# 각 항목: 언론사명, 카테고리, 주소, 사용 여부", "feeds:"]
    for r in sorted(results, key=lambda x: (x["publisher"], x["category"])):
        usable = r["status"] in ("OK", "NODATE")
        lines.append(f"  - publisher: {json.dumps(r['publisher'], ensure_ascii=False)}")
        lines.append(f"    category: {json.dumps(r['category'], ensure_ascii=False)}")
        lines.append(f"    url: {r['url_used']}")
        lines.append(f"    enabled: {'true' if usable else 'false'}")
        if r["status"] == "NODATE":
            lines.append("    # 항목에 날짜 없음 → 수집 시각(first-seen)을 발행 시각 대신 사용")
        if not usable:
            lines.append(f"    # 제외 사유: {r['status']} {r['reason']}")
        lines.append(f"    robots: {json.dumps(fmt_robots(r['robots']), ensure_ascii=False)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- 메인
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=str(ROOT / "data/feed_specs.csv"))
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    kw = Keywords(ROOT / "keywords.yaml")
    fc = cfg["feed_check"]
    wd = fc["window_days"]
    now = datetime.now(timezone.utc)

    want = set(fc["categories"])
    specs = []
    with open(args.csv, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if want & set(row["categories"].split("|")):
                specs.append(row)
    print(f"대상 피드 {len(specs)}개 / 언론사 {len({s['publisher'] for s in specs})}곳", file=sys.stderr)

    fetcher = Fetcher(cfg)
    robots = RobotsCache(fetcher, cfg["http"]["bot_token"])
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        results = list(ex.map(lambda s: check_feed(s, cfg, fetcher, robots, kw, now), specs))

    # ---- 집계 (정상 = 날짜 있고 7일 안에 갱신된 피드)
    healthy = [r for r in results if r["status"] == "OK"]
    healthy_pubs = {r["publisher"] for r in healthy}
    nodate = [r for r in results if r["status"] == "NODATE"]

    uniq: dict[str, dict] = {}
    for r in healthy:
        for it in r["recent"]:
            u = uniq.setdefault(it["link"], {**it, "publishers": set()})
            u["publishers"].add(r["publisher"])
    items = list(uniq.values())
    passed_items = [u for u in items if u["kw"]]
    core_items = [u for u in items if u["core"]]
    core_any = [u for u in items if u["core"] or u["desc_core"]]
    kw_counter = Counter(k for u in passed_items for k in u["kw"])
    probe_counter = Counter(k for u in items for k in u["probe"])
    pass_pubs = {p for u in passed_items for p in u["publishers"]}
    core_pubs = {p for u in core_items for p in u["publishers"]}

    # 언론사별 수집 범위로 보정한 하루 추정치
    adj = {"pass": 0.0, "core": 0.0, "core_any": 0.0}
    by_pub: dict[str, dict] = {}
    for p in sorted({r["publisher"] for r in results}):
        rs = [r for r in results if r["publisher"] == p]
        hs = [r for r in rs if r["status"] == "OK"]
        plinks = {it["link"] for r in hs for it in r["recent"]}
        span = max([r["span_days"] for r in hs] or [0])
        span = max(span, 0.25)  # 6시간 미만 범위로 나누어 과대 추정하지 않도록 하한
        if hs:
            adj["pass"] += sum(1 for l in plinks if uniq[l]["kw"]) / span
            adj["core"] += sum(1 for l in plinks if uniq[l]["core"]) / span
            adj["core_any"] += sum(1 for l in plinks if uniq[l]["core"] or uniq[l]["desc_core"]) / span
        rb = Counter()
        for r in rs:
            for k in ("allowed", "blocked", "unknown"):
                rb[k] += r["robots"][k]
        tot = sum(rb.values())
        robots_txt = (f"허용 {rb['allowed']}/{tot}" if tot else "-") + (
            f", 금지 {rb['blocked']}" if rb["blocked"] else "") + (
            f", 불명 {rb['unknown']}" if rb["unknown"] else "")
        notes_p = sorted({f"{r['status']}: {r['reason']}" for r in rs if r["status"] != "OK"})
        by_pub[p] = {
            "feeds": len(rs), "healthy": len(hs),
            "pass": sum(1 for l in plinks if uniq[l]["kw"]),
            "core": sum(1 for l in plinks if uniq[l]["core"]),
            "robots": robots_txt, "note": "; ".join(notes_p),
        }

    per_day = len(passed_items) / wd
    notes = []
    partial = [f"{r['publisher']} {r['title']}({r['span_days']}일)" for r in healthy if r["span_days"] < wd]
    if partial:
        notes.append(f"항목이 7일치에 못 미치는 정상 피드 {len(partial)}개 → '지시서 방식' 하루 평균은 과소 추정이다. "
                     "정확한 값은 1단계에서 하루 3회 수집을 며칠 누적해야 나온다.")
    future = [f"{r['publisher']} {r['title']}({r['future_items']}건)" for r in healthy if r["future_items"]]
    if future:
        notes.append("타임존 보정 후에도 미래 날짜인 항목이 있는 피드(서버 시계·발행 예약 의심): " + ", ".join(future))
    notes.append("타임존 표기 없는 날짜(시사저널·프레시안·여성신문·미디어오늘·시사IN 등)는 한국 시각으로 간주했다.")
    notes.append("키워드 필터는 지시서 3-2대로 '제목'만 본다. '지급·인상·인하·개편·시행'은 지원금과 무관한 기사"
                 "(금리 인하, 요금 인상 등)도 통과시키므로 통과 건수 ≠ 쓸 수 있는 소재 건수다.")
    notes.append("같은 언론사의 전체·경제·사회 피드는 링크 기준으로 중복 제거했다. "
                 "서로 다른 언론사의 같은 주제 기사는 별개 건수로 센다.")
    notes.append("스냅샷 시각이 평일 낮이라 기사 발행이 몰리는 시간대다. 보정 추정은 과대·과소 양쪽 오차가 있다.")

    criteria = [
        ("정상 피드 수", f"{fc['min_healthy_feeds']}개 이상", f"{len(healthy)}개 (+날짜없음 {len(nodate)}개)",
         len(healthy) >= fc["min_healthy_feeds"]),
        ("정상 피드의 언론사 수", f"{fc['min_healthy_publishers']}곳 이상", f"{len(healthy_pubs)}곳",
         len(healthy_pubs) >= fc["min_healthy_publishers"]),
        ("키워드 통과 기사/일 (지시서 방식 ÷7)", f"{fc['min_keyword_per_day']}건 이상", f"{per_day:.1f}건",
         per_day >= fc["min_keyword_per_day"]),
    ]
    nd_items = [it for r in nodate for it in r["recent"]]
    S = {
        "n_feeds": len(results), "n_publishers": len({r["publisher"] for r in results}),
        "criteria": criteria, "passed": all(c[3] for c in criteria),
        "n_unique": len(items), "n_pass": len(passed_items), "n_core": len(core_items),
        "n_core_any": len(core_any), "per_day": per_day,
        "adj_pass": adj["pass"], "adj_core": adj["core"], "adj_core_any": adj["core_any"],
        "kw_counter": kw_counter, "probe_counter": probe_counter,
        "pass_publishers": len(pass_pubs), "core_publishers": len(core_pubs),
        "nodate_feeds": ", ".join(f"{r['publisher']} {r['title']}" for r in nodate) or "-",
        "nodate_items": len(nd_items),
        "nodate_pass": sum(1 for it in nd_items if it["kw"]) if nodate else None,
        "by_publisher": by_pub, "notes": notes,
    }

    (ROOT / "reports").mkdir(exist_ok=True)
    (ROOT / "data").mkdir(exist_ok=True)
    out = {
        "run_at": now.isoformat(), "criteria": [list(c) for c in criteria],
        "passed": S["passed"], "per_day_literal": per_day,
        "per_day_adjusted": {k: round(v, 1) for k, v in adj.items()},
        "passed_items": sorted(
            ({"title": u["title"], "link": u["link"], "pub": u["pub"], "kw": u["kw"],
              "core": u["core"], "desc_core": u["desc_core"], "publishers": sorted(u["publishers"])}
             for u in passed_items), key=lambda x: x["pub"] or "", reverse=True),
        "core_any_items": sorted(
            ({"title": u["title"], "link": u["link"], "pub": u["pub"], "core": u["core"],
              "desc_core": u["desc_core"], "publishers": sorted(u["publishers"])}
             for u in core_any), key=lambda x: x["pub"] or "", reverse=True),
        "feeds": results,
    }
    (ROOT / "data/feed_check_result.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    (ROOT / "reports/stage0_feed_check.md").write_text(
        build_report(results, cfg, now, S), encoding="utf-8")
    write_feeds_yaml(results, ROOT / "feeds.yaml")

    print(build_report(results, cfg, now, S))
    return 0 if S["passed"] else 2


if __name__ == "__main__":
    sys.exit(main())
