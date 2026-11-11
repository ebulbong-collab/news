#!/usr/bin/env python3
"""RSS 수집 + 키워드 필터 (지시서 3-1, 3-2).

feeds.yaml 에서 enabled 인 피드를 읽어 키워드를 통과한 기사의 메타데이터만 저장한다.
  data/candidates.json   통과 기사 (제목·링크·언론사·발행시각). 기사 본문·요약은 저장하지 않는다.
  data/seen.json         이미 본 링크 {link: first_seen}. 30일분만 유지.
  data/collect_log.jsonl 실행마다 한 줄씩 남기는 통계 (1단계 관찰용)

피드 하나가 실패해도 나머지는 계속 진행한다. 정상 수집된 피드가 min_ok_feeds 보다 적으면 비정상 종료한다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import feedparser

from common import (ROOT, Fetcher, Keywords, clean_text, clean_title, entry_dt, iso_kst,
                    load_config, load_yaml, norm_link, parse_iso, read_json, write_json)

DATA = ROOT / "data"


def fetch_feed(feed: dict, fetcher: Fetcher, kw: Keywords, now: datetime, window: timedelta) -> dict:
    out = {"publisher": feed["publisher"], "category": feed["category"], "url": feed["url"],
           "ok": False, "error": None, "entries": 0, "in_window": 0, "items": [], "v1_pass": 0,
           "oldest": None}
    r, err = fetcher.get(feed["url"])
    if r is None:
        out["error"] = err
        return out
    if r.status_code != 200:
        out["error"] = f"HTTP {r.status_code}"
        return out
    d = feedparser.parse(r.content)
    if not d.entries:
        out["error"] = "항목 0개"
        return out
    out["ok"] = True
    out["entries"] = len(d.entries)
    for e in d.entries:
        link = norm_link(e.get("link", ""))
        if not link:
            continue
        dt = entry_dt(e)
        if dt is not None and (out["oldest"] is None or dt < out["oldest"]):
            out["oldest"] = dt
        if dt is not None and (dt < now - window or dt > now + timedelta(days=1)):
            continue  # 수집 범위 밖. 날짜 없는 피드(한겨레)는 수집 시각을 발행 시각 대신 쓴다.
        out["in_window"] += 1
        title = clean_title(e.get("title", ""))
        desc = clean_text(e.get("summary") or e.get("description") or "")
        m = kw.match(title, desc)
        out["v1_pass"] += int(m.v1_passed)
        if not m.passed:
            continue
        out["items"].append({
            "id": hashlib.sha1(link.encode()).hexdigest()[:10],
            "publisher": feed["publisher"], "feed": feed["category"],
            "title": title, "link": link,
            "pub": iso_kst(dt) if dt else None,
            "reason": m.reason, "hits": m.hits,
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="파일을 쓰지 않고 통계만 출력")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    cfg = load_config()
    cc = cfg["collect"]
    kw = Keywords()
    now = datetime.now(timezone.utc)
    window = timedelta(hours=cc["window_hours"])

    feeds = [f for f in load_yaml("feeds.yaml")["feeds"] if f["enabled"]]
    fetcher = Fetcher(cfg, cfg["http"]["user_agent_feed"])
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        results = list(ex.map(lambda f: fetch_feed(f, fetcher, kw, now, window), feeds))

    seen: dict[str, str] = read_json(DATA / "seen.json", {})
    cands: list[dict] = read_json(DATA / "candidates.json", [])
    known = {c["link"] for c in cands} | set(seen)

    new_items, dup = [], 0
    for res in results:
        for it in res["items"]:
            if it["link"] in known:
                dup += 1
                continue
            known.add(it["link"])
            it["first_seen"] = iso_kst(now)
            new_items.append(it)
    cands += new_items

    # 보관 기간이 지난 후보·seen 정리
    cutoff = now - timedelta(hours=cc["candidates_retention_hours"])
    cands = [c for c in cands if parse_iso(c["pub"] or c["first_seen"]) >= cutoff]
    for it in new_items:
        seen[it["link"]] = it["first_seen"]
    seen_cut = now - timedelta(days=cc["seen_retention_days"])
    seen = {l: t for l, t in seen.items() if parse_iso(t) >= seen_cut}
    cands.sort(key=lambda c: c["pub"] or c["first_seen"], reverse=True)

    ok = [r for r in results if r["ok"]]
    failed = [{"publisher": r["publisher"], "category": r["category"], "error": r["error"]}
              for r in results if not r["ok"]]
    # 언론사별 수집 범위: 그 언론사 피드들이 보여주는 가장 오래된 항목까지의 시간(시간 단위, 최대 window)
    coverage: dict[str, float] = {}
    for r in ok:
        if r["oldest"] is None:
            continue
        h = min((now - r["oldest"]).total_seconds() / 3600, cc["window_hours"])
        coverage[r["publisher"]] = round(max(coverage.get(r["publisher"], 0), h), 1)
    stats = {
        "run_at": iso_kst(now), "feeds": len(feeds), "feeds_ok": len(ok), "feeds_failed": failed,
        "entries": sum(r["entries"] for r in ok), "in_window": sum(r["in_window"] for r in ok),
        "pass_total": sum(len(r["items"]) for r in ok), "duplicates": dup, "new": len(new_items),
        "new_by_reason": dict(Counter(i["reason"] for i in new_items)),
        "v1_pass_total": sum(r["v1_pass"] for r in ok),
        "candidates_kept": len(cands),
        "coverage_hours": coverage,
    }

    print(json.dumps(stats, ensure_ascii=False, indent=1), file=sys.stderr)
    for f in failed:
        print(f"[실패] {f['publisher']} {f['category']}: {f['error']}", file=sys.stderr)

    if not args.dry_run:
        write_json(DATA / "candidates.json", cands)
        write_json(DATA / "seen.json", seen)
        with open(DATA / "collect_log.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(stats, ensure_ascii=False) + "\n")

    if len(ok) < cc["min_ok_feeds"]:
        print("정상 수집된 피드가 너무 적다 — 실패 종료", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
