#!/usr/bin/env python3
"""주제 묶기 (지시서 3-3).

data/candidates.json 의 최근 window_hours 이내 기사 제목을 Claude(저가 모델)에 넘겨 묶음을 받는다.
서로 다른 언론사 min_publishers 곳 이상의 기사가 있는 묶음만 글 후보(eligible)가 된다.
1곳뿐인 묶음은 waiting 으로 두고, 후보 보관 기간이 지나면 candidates.json 에서 사라지며 폐기된다.

  data/clusters.json       이번 실행의 묶음 결과
  data/cluster_log.jsonl   실행마다 한 줄 통계 (1단계 관찰용)

API 호출 없이 이미 만든 묶음 JSON 을 다시 적용하려면 --groups-file 을 쓴다(재현·검증용).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from common import ROOT, iso_kst, load_config, parse_iso, read_json, write_json

DATA = ROOT / "data"

GROUP_SCHEMA = {
    "type": "object",
    "properties": {
        "groups": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "policy_related": {"type": "boolean"},
                    "ids": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["label", "policy_related", "ids"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["groups"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------- 입력 만들기
def select_window(cands: list[dict], now: datetime, hours: int, max_titles: int) -> list[dict]:
    cut = now - timedelta(hours=hours)
    rows = [c for c in cands if parse_iso(c["pub"] or c["first_seen"]) >= cut]
    rows.sort(key=lambda c: c["pub"] or c["first_seen"], reverse=True)
    if len(rows) > max_titles:
        print(f"[경고] 후보 {len(rows)}건이 상한 {max_titles}건을 넘어 최근 순으로 자른다", file=sys.stderr)
        rows = rows[:max_titles]
    return rows


def render_titles(rows: list[dict]) -> str:
    return "\n".join(f"{i} | {r['publisher']} | {r['title']}" for i, r in enumerate(rows, 1))


# ---------------------------------------------------------------- Claude 호출
def call_model(rows: list[dict], cfg: dict) -> tuple[dict, dict]:
    """(groups JSON, usage). 재시도·거절 처리는 llm.call_structured 가 맡는다."""
    from llm import call_structured  # API 를 쓰지 않는 경로(--groups-file)에서는 SDK 가 없어도 된다

    cc = cfg["cluster"]
    return call_structured(
        cfg, model=cfg["models"]["cluster_extract"],
        system=(ROOT / "prompts" / "cluster.md").read_text(encoding="utf-8"),
        user=render_titles(rows), schema=GROUP_SCHEMA,
        effort=cc["model_effort"], max_tokens=cc["max_tokens"])


# ---------------------------------------------------------------- 후처리
def normalize_groups(groups: list[dict], n: int) -> tuple[list[dict], dict]:
    """모델 출력 검증: 범위 밖 번호 제거, 중복 번호는 첫 묶음만 인정, 빠진 번호는 단독 묶음으로 보완."""
    fixes = {"out_of_range": 0, "duplicate": 0, "missing": 0}
    used: set[int] = set()
    out = []
    for g in groups:
        ids = []
        for i in g["ids"]:
            if not 1 <= i <= n:
                fixes["out_of_range"] += 1
            elif i in used:
                fixes["duplicate"] += 1
            else:
                used.add(i)
                ids.append(i)
        if ids:
            out.append({"label": g["label"], "policy_related": g["policy_related"], "ids": ids})
    for i in range(1, n + 1):
        if i not in used:
            fixes["missing"] += 1
            out.append({"label": "(모델이 빠뜨린 기사)", "policy_related": False, "ids": [i]})
    return out, fixes


def build_clusters(groups: list[dict], rows: list[dict], topics: list[dict], min_pub: int) -> list[dict]:
    covered = {}  # link -> topic id
    for t in topics:
        for l in t.get("source_links", []):
            covered[l] = t["id"]
    clusters = []
    for g in groups:
        arts = [rows[i - 1] for i in g["ids"]]
        pubs = sorted({a["publisher"] for a in arts})
        links = {a["link"] for a in arts}
        hit_topics = {covered[l] for l in links if l in covered}
        if len(pubs) < min_pub:
            action = "waiting"
        elif not hit_topics:
            action = "new"
        elif links <= set(covered):
            action = "covered"
        else:
            action = "update"
        clusters.append({
            "label": g["label"], "policy_related": g["policy_related"],
            "publishers": pubs, "n_publishers": len(pubs),
            "eligible": len(pubs) >= min_pub, "action": action,
            "existing_topics": sorted(hit_topics),
            "articles": [{"id": a["id"], "publisher": a["publisher"], "title": a["title"],
                          "link": a["link"], "pub": a["pub"]} for a in arts],
        })
    clusters.sort(key=lambda c: (-c["n_publishers"], -len(c["articles"])))
    for n, c in enumerate(clusters, 1):
        c["cluster_id"] = f"c{n:02d}"
    return clusters


# ---------------------------------------------------------------- 보고서
def write_report(path, result: dict, collect_log: list[dict]) -> None:
    cl = result["clusters"]
    S = result["stats"]
    L = ["# 1단계 결과 — 수집·필터·묶기", ""]
    L.append(f"- 묶기 실행: {result['run_at']} / 모델 `{result['model']}` / 대상 {S['n_candidates']}건 "
             f"(최근 {result['window_hours']}시간)")
    if result.get("source") != "model":
        L.append(f"- **주의: 묶음의 출처는 \"{result['source']}\"이며, 운영 경로(Claude API)의 결과가 아니다.**")
    L.append("")
    L.append("## 요약")
    L.append("")
    L.append("| 항목 | 값 |")
    L.append("|---|---|")
    L.append(f"| 키워드 통과 후보 | {S['n_candidates']}건 |")
    L.append(f"| 묶음 수 | {S['n_clusters']}개 (단독 {S['n_single_article']}개) |")
    L.append(f"| 서로 다른 언론사 {result['min_publishers']}곳 이상 묶음 | **{S['n_eligible']}개** |")
    L.append(f"| 그중 지원금·제도 관련(`policy_related`) | **{S['n_draft_candidates']}개** |")
    L.append(f"| 1곳뿐이라 대기(waiting) | {S['n_waiting']}개 ({S['n_waiting_policy']}개가 제도 관련) |")
    L.append("")
    L.append("## 글 후보 묶음 (언론사 2곳 이상)")
    L.append("")
    for c in cl:
        if not c["eligible"]:
            continue
        tag = "제도 관련" if c["policy_related"] else "제도 무관"
        L.append(f"### {c['cluster_id']} {c['label']} — {c['n_publishers']}개 언론사 · 기사 {len(c['articles'])}건 · {tag} · {c['action']}")
        L.append("")
        for a in c["articles"]:
            when = a["pub"][5:16].replace("T", " ") if a["pub"] else "날짜없음"
            L.append(f"- [{a['publisher']}] {a['title']} ({when})")
        L.append("")
    if not any(c["eligible"] for c in cl):
        L.append("_해당 없음_")
        L.append("")
    L.append("## 대기 중인 묶음 (1개 언론사)")
    L.append("")
    L.append("| 제도 관련 | 언론사 | 제목 |")
    L.append("|---|---|---|")
    for c in cl:
        if c["eligible"]:
            continue
        for a in c["articles"]:
            L.append(f"| {'O' if c['policy_related'] else '-'} | {a['publisher']} | {a['title'][:60]} |")
    L.append("")
    if collect_log:
        L.append("## 수집 이력")
        L.append("")
        L.append("| 실행 | 정상 피드 | 읽은 항목 | 키워드 통과 | 신규 | 원안(v1) 통과 |")
        L.append("|---|---|---|---|---|---|")
        for r in collect_log:
            L.append(f"| {r['run_at'][5:16].replace('T', ' ')} | {r['feeds_ok']}/{r['feeds']} | {r['entries']} | "
                     f"{r['pass_total']} | {r['new']} | {r['v1_pass_total']} |")
        L.append("")
        cov = collect_log[-1].get("coverage_hours", {})
        if cov:
            L.append("가장 최근 수집에서 언론사별로 실제 관찰된 범위(시간): "
                     + ", ".join(f"{p} {h:g}" for p, h in sorted(cov.items(), key=lambda x: x[1])))
            L.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(L) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- 메인
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--groups-file", help="API 를 부르지 않고 이 JSON({'groups': [...]})을 묶음으로 쓴다")
    ap.add_argument("--source-note", default="--groups-file 로 주입한 묶음", help="--groups-file 사용 시 보고서에 적을 출처")
    ap.add_argument("--report", help="보고서(.md) 경로")
    ap.add_argument("--dry-run", action="store_true", help="결과 파일을 쓰지 않는다")
    args = ap.parse_args()

    cfg = load_config()
    cc = cfg["cluster"]
    now = datetime.now(timezone.utc)
    rows = select_window(read_json(DATA / "candidates.json", []), now,
                         cfg["collect"]["window_hours"], cc["max_titles"])
    if len(rows) < 2:
        print("묶을 후보가 2건 미만이다 — 종료", file=sys.stderr)
        return 0

    usage = None
    if args.groups_file:
        raw = json.loads(open(args.groups_file, encoding="utf-8").read())
        source = args.source_note
    else:
        raw, usage = call_model(rows, cfg)
        source = "model"
    groups, fixes = normalize_groups(raw["groups"], len(rows))
    if any(fixes.values()):
        print(f"[보정] 모델 출력 보정: {fixes}", file=sys.stderr)

    clusters = build_clusters(groups, rows, read_json(DATA / "topics.json", []), cc["min_publishers"])
    elig = [c for c in clusters if c["eligible"]]
    wait = [c for c in clusters if not c["eligible"]]
    stats = {
        "n_candidates": len(rows), "n_clusters": len(clusters),
        "n_single_article": sum(1 for c in clusters if len(c["articles"]) == 1),
        "n_eligible": len(elig),
        "n_draft_candidates": sum(1 for c in elig if c["policy_related"] and c["action"] != "covered"),
        "n_waiting": len(wait), "n_waiting_policy": sum(1 for c in wait if c["policy_related"]),
        "fixes": fixes,
    }
    result = {"run_at": iso_kst(now), "model": cfg["models"]["cluster_extract"], "source": source,
              "window_hours": cfg["collect"]["window_hours"], "min_publishers": cc["min_publishers"],
              "stats": stats, "usage": usage, "clusters": clusters}

    print(json.dumps({"stats": stats, "usage": usage}, ensure_ascii=False, indent=1), file=sys.stderr)
    for c in elig:
        print(f"  {c['cluster_id']} [{c['n_publishers']}곳/{len(c['articles'])}건/"
              f"{'제도' if c['policy_related'] else '무관'}/{c['action']}] {c['label']}", file=sys.stderr)

    if not args.dry_run:
        write_json(DATA / "clusters.json", result)
        with open(DATA / "cluster_log.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"run_at": result["run_at"], "source": source, **stats,
                                "eligible": [c["label"] for c in elig]}, ensure_ascii=False) + "\n")
    if args.report:
        log = [json.loads(l) for l in (DATA / "collect_log.jsonl").read_text(encoding="utf-8").splitlines()] \
            if (DATA / "collect_log.jsonl").exists() else []
        write_report(Path(args.report), result, log)
    return 0


if __name__ == "__main__":
    sys.exit(main())
