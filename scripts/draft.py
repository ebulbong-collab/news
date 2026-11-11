#!/usr/bin/env python3
"""초안 생성: 본문 가져오기 → 사실 추출 → 글 작성 → 유사도·분량 검사 (지시서 3-4 ~ 3-6).

data/clusters.json 의 글 후보 묶음(서로 다른 언론사 2곳 이상, 제도 관련)을 처리한다.
결과는 --out 아래 묶음별 폴더에 둔다. 기사 본문은 메모리에서만 쓰고 어디에도 저장하지 않는다.

  <out>/<cluster>/facts.json     추출한 사실 항목 (본문·인용 없음)
  <out>/<cluster>/post.md        통과한 초안  (탈락 시 rejected.md)
  <out>/<cluster>/result.json    상태·분량·유사도·사용 토큰·원문 확보 방식
  <out>/summary.md               전체 요약

초안 PR 생성과 topics.json 기록은 3단계(PR 흐름)에서 붙인다. 여기서는 만들지 않는다.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import anthropic

import extract
import similarity
import write
from common import KST, ROOT, load_config, read_json, write_json
from fetch_body import fetch_articles, pick_articles
from llm import LLMError


# ---------------------------------------------------------------- 선택
def select_clusters(clusters: list[dict], ids: list[str], dev_single: bool, max_n: int) -> list[dict]:
    if ids:
        chosen = [c for c in clusters if c["cluster_id"] in ids]
    else:
        chosen = [c for c in clusters if c["eligible"] and c["policy_related"] and c["action"] != "covered"]
        if dev_single:  # 개발용: 1개 언론사 묶음도 시험한다(발행 대상 아님)
            chosen += [c for c in clusters if not c["eligible"] and c["policy_related"]]
    return chosen[:max_n]


def cost_usd(usages: list[dict], cfg: dict) -> float:
    total = 0.0
    for u in usages:
        pin, pout = cfg["prices_usd_per_mtok"].get(u["model"], (0, 0))
        total += u["input_tokens"] * pin / 1e6 + u["output_tokens"] * pout / 1e6
    return total


def build_feedback(issues: list[str], post: write.Post, flagged: list[str], lo: int, hi: int) -> str:
    """재작성 지시. 원문은 넣지 않고, 문제가 된 '초안의' 문장만 돌려준다."""
    lines = ["이전 초안에 다음 문제가 있었다: " + " / ".join(issues)]
    if post.chars < lo:
        lines.append(f"JSON 의 사실을 빠짐없이 풀어 쓰되 분량을 {lo}~{hi}자로 늘려라. 사실을 새로 만들어서 늘리지 마라.")
    elif post.chars > hi:
        lines.append(f"분량을 {lo}~{hi}자로 줄여라.")
    if flagged:
        lines.append("다음 문장은 표현이 겹친다고 판정됐다. 같은 표현·어순을 쓰지 말고 완전히 다르게 써라:\n"
                     + "\n".join(f"- {s}" for s in flagged))
    lines.append("사실 값은 그대로 두고 문장 구조와 표현을 바꿔라.")
    return "\n".join(lines)


# ---------------------------------------------------------------- 묶음 1개 처리
def process_cluster(c: dict, cfg: dict, dev_single: bool, outdir: Path) -> dict:
    d = cfg["draft"]
    lo, hi = d["post_chars_target"]
    threshold = cfg["limits"]["similarity_threshold"]
    cdir = outdir / (("DEV-" if (dev_single and not c["eligible"]) else "") + c["cluster_id"])
    cdir.mkdir(parents=True, exist_ok=True)
    res: dict = {"cluster_id": c["cluster_id"], "label": c["label"], "status": "", "reason": "",
                 "dev_single_source": bool(dev_single and not c["eligible"]),
                 "usages": [], "articles": [], "attempts": []}

    def finish(status: str, reason: str = "") -> dict:
        res.update(status=status, reason=reason)
        res["cost_usd_est"] = round(cost_usd(res["usages"], cfg), 4)
        write_json(cdir / "result.json", res)
        return res

    picked = pick_articles(c["articles"], cfg["limits"]["max_articles_per_cluster"])
    arts = fetch_articles(picked, cfg)
    res["articles"] = [a.info() for a in arts]
    usable = [a for a in arts if a.source != "none"]
    pubs = {a.publisher for a in usable}
    if not usable:
        return finish("skipped", "no_text: 본문도 RSS 요약도 확보하지 못했다")
    if len(pubs) < cfg["cluster"]["min_publishers"] and not res["dev_single_source"]:
        return finish("skipped", f"insufficient_sources: 원문을 확보한 언론사 {len(pubs)}곳 (기준 {cfg['cluster']['min_publishers']}곳)")

    facts, u = extract.extract_facts(usable, cfg)
    res["usages"].append(u)
    write_json(cdir / "facts.json", facts)
    if facts["skip"]:
        return finish("skipped", f"irrelevant: {facts['이유']}")
    if extract.is_thin(facts, d["thin_missing_limit"]):
        return finish("skipped", "thin_facts: 대상·금액·기간 중 기사에 없는 항목이 " + f"{d['thin_missing_limit']}개 이상 ({', '.join(facts['기사에_없는_항목'])})")
    res["ungrounded_numbers"] = extract.ground_check(facts, usable)

    sources = {f"{a.publisher}#{i}": a.text for i, a in enumerate(usable)}
    feedback = None
    post = None
    sim: dict = {}
    for attempt in range(d["max_rewrite"] + 1):
        w, u = write.write_post(facts, cfg, feedback)
        res["usages"].append(u)
        post = write.build_post(facts, w, datetime.now(KST))
        sim = similarity.check(post.prose, sources, threshold)
        res["attempts"].append({"attempt": attempt + 1, "chars": post.chars, "similarity": sim["union_ratio"],
                                "problems": post.problems})
        issues = []
        if post.problems:
            issues.append("구성 문제: " + "; ".join(post.problems))
        if not lo <= post.chars <= hi:
            issues.append(f"분량 {post.chars}자 (목표 {lo}~{hi}자)")
        if not sim["passed"]:
            issues.append(f"원문과 5어절 겹침 {sim['union_ratio']:.1%} (기준 {threshold:.0%})")
        if not issues:
            break
        if attempt < d["max_rewrite"]:
            feedback = build_feedback(issues, post, similarity.flagged_sentences(post.prose, sources), lo, hi)

    res["similarity"] = sim
    res["chars"] = post.chars
    res["slug"] = post.slug
    in_range = lo <= post.chars <= hi
    if post.problems:
        (cdir / "rejected.md").write_text(post.markdown, encoding="utf-8")
        return finish("discarded", "구성 문제: " + "; ".join(post.problems))
    if not sim["passed"]:
        (cdir / "rejected.md").write_text(post.markdown, encoding="utf-8")
        return finish("discarded", f"similarity: 재작성 {d['max_rewrite']}회 후에도 겹침 {sim['union_ratio']:.1%} > {threshold:.0%}")
    if post.chars < d["post_chars_min"]:
        (cdir / "rejected.md").write_text(post.markdown, encoding="utf-8")
        return finish("discarded", f"too_short: {post.chars}자 < {d['post_chars_min']}자")
    (cdir / "post.md").write_text(post.markdown, encoding="utf-8")
    return finish("drafted", "" if in_range else f"분량이 목표 범위를 벗어났다({post.chars}자, 목표 {lo}~{hi}자)")


# ---------------------------------------------------------------- 요약
def write_summary(path: Path, results: list[dict], cfg: dict, aborted: str | None) -> None:
    L = ["# 초안 생성 결과", "",
         f"- 실행: {datetime.now(KST):%Y-%m-%d %H:%M} KST",
         f"- 모델: 사실 추출 `{cfg['models']['cluster_extract']}` / 글 작성 `{cfg['models']['write']}`",
         f"- 유사도 기준: 5어절 겹침 {cfg['limits']['similarity_threshold']:.0%} 이하 / 분량 목표 {cfg['draft']['post_chars_target'][0]}~{cfg['draft']['post_chars_target'][1]}자"]
    if aborted:
        L += ["", f"**중단됨: {aborted}**"]
    total = sum(r.get("cost_usd_est", 0) for r in results)
    L += ["", "| 묶음 | 상태 | 원문 확보 | 분량 | 겹침 | 시도 | 비용(추정) |", "|---|---|---|---|---|---|---|"]
    for r in results:
        kinds = [a["source"] for a in r["articles"]]
        got = f"본문 {kinds.count('body')} · RSS {kinds.count('rss_summary')} · 없음 {kinds.count('none')}"
        tag = " (개발용·1개 언론사)" if r["dev_single_source"] else ""
        L.append(f"| {r['cluster_id']} {r['label']}{tag} | **{r['status']}** | {got} | {r.get('chars', '-')} | "
                 f"{(str(round(r['similarity']['union_ratio'] * 100, 1)) + '%') if r.get('similarity') else '-'} | "
                 f"{len(r['attempts']) or '-'} | ${r.get('cost_usd_est', 0):.4f} |")
    L += ["", f"비용 합계(추정): **${total:.4f}** — 토큰 수 × 단가(config.yaml `prices_usd_per_mtok`)", ""]
    for r in results:
        L += [f"## {r['cluster_id']} {r['label']}", "", f"- 상태: **{r['status']}** {r['reason']}"]
        for a in r["articles"]:
            L.append(f"- 원문: [{a['publisher']}] {a['source']} ({a['chars']}자, robots {a['robots']}) {a['note']}")
        if r.get("ungrounded_numbers"):
            L.append("- 원문에서 찾지 못한 숫자(검수 시 확인): " + ", ".join(f"{x['항목']}:{x['값']}" for x in r["ungrounded_numbers"]))
        for at in r["attempts"]:
            L.append(f"- 시도 {at['attempt']}: {at['chars']}자, 겹침 {at['similarity'] * 100:.1f}%" + (f", 문제 {at['problems']}" if at["problems"] else ""))
        for u in r["usages"]:
            L.append(f"- 사용: {u['model']} (응답 {u['served_model']}) in {u['input_tokens']} / out {u['output_tokens']} · {u['request_id']}")
        L.append("")
    path.write_text("\n".join(L) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- 메인
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clusters", default="", help="처리할 묶음 ID(쉼표 구분). 비우면 자동 선택")
    ap.add_argument("--dev-single-source", action="store_true", help="개발용: 1개 언론사 묶음도 시험(발행 대상 아님)")
    ap.add_argument("--max", type=int, default=None, help="최대 묶음 수 (기본: limits.max_drafts_per_day)")
    ap.add_argument("--out", default="data/tmp/drafts")
    ap.add_argument("--clusters-file", default="data/clusters.json")
    args = ap.parse_args()

    cfg = load_config()
    max_n = args.max or cfg["limits"]["max_drafts_per_day"]
    clusters = read_json(ROOT / args.clusters_file, {}).get("clusters", [])
    ids = [x.strip() for x in args.clusters.split(",") if x.strip()]
    todo = select_clusters(clusters, ids, args.dev_single_source, max_n)
    outdir = ROOT / args.out
    outdir.mkdir(parents=True, exist_ok=True)
    print(f"처리할 묶음 {len(todo)}개: {[c['cluster_id'] for c in todo]}", file=sys.stderr)

    results, aborted = [], None
    for c in todo:
        try:
            r = process_cluster(c, cfg, args.dev_single_source, outdir)
        except LLMError as e:  # 한 묶음의 거절·끊김은 그 묶음만 실패로 두고 계속한다
            r = {"cluster_id": c["cluster_id"], "label": c["label"], "status": "error", "reason": str(e),
                 "dev_single_source": bool(args.dev_single_source and not c["eligible"]),
                 "usages": [], "articles": [], "attempts": []}
        except (anthropic.APIStatusError, anthropic.APIConnectionError) as e:
            aborted = f"API 오류로 중단({type(e).__name__}) — 재시도 후에도 실패해 그날은 여기서 멈춘다"
            print(aborted, file=sys.stderr)
            break
        results.append(r)
        print(f"  {r['cluster_id']}: {r['status']} {r['reason']}", file=sys.stderr)

    write_summary(outdir / "summary.md", results, cfg, aborted)
    print(json.dumps({"results": [{k: r[k] for k in ("cluster_id", "status", "reason")} for r in results]},
                     ensure_ascii=False, indent=1))
    return 1 if aborted else 0


if __name__ == "__main__":
    sys.exit(main())
