#!/usr/bin/env python3
"""초안 PR 생성 (지시서 3-7).

draft.py 가 만든 통과 초안을 `content/posts/` 에 넣은 브랜치(`drafts/YYYY-MM-DD`)를 만들고 PR 을 연다.
- PR 을 병합하면 발행된다. 일부만 올릴 때는 PR 에서 해당 파일을 지우고 병합한다. 자동 병합은 만들지 않는다.
- 기사 1곳짜리(개발용) 초안은 --allow-dev 없이는 PR 에 넣지 않는다(§1: 서로 다른 언론사 2곳 이상만 글로 만든다).
- topics.json 도 같은 브랜치에서 갱신한다. 병합되기 전까지는 '다룬 주제'로 치지 않는다(cluster.py 는 발행된 파일이 있는
  주제만 본다).

필요 권한: contents: write, pull-requests: write, 그리고 저장소 설정의
Settings → Actions → General → "Allow GitHub Actions to create and approve pull requests".
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from common import KST, ROOT, read_json, write_json

CHECKLIST = """### 검수 체크리스트

- [ ] 금액과 신청 기간을 원문 기사에서 직접 확인했다
- [ ] 불일치 항목이 있으면 주관기관 사이트에서 확인했다
- [ ] 기사 문장을 그대로 옮긴 부분이 없다
- [ ] 이미 마감된 사업이 아니다
- [ ] 제목이 내용보다 과장되지 않았다

병합하면 발행됩니다. 일부만 올릴 때는 PR 에서 해당 파일을 삭제하고 병합하세요. 자동 병합은 설정하지 않았습니다."""

FACT_ROWS = ["제도명", "주관기관", "지역명", "분야", "대상", "금액", "신청기간", "신청방법", "필요서류", "문의처", "시행일"]
PR_BODY_LIMIT = 60000   # GitHub PR 본문 상한(65,536자)보다 여유를 둔다


@dataclass
class Draft:
    cluster_id: str
    label: str
    slug: str
    markdown: str
    facts: dict
    result: dict
    dev: bool
    is_update: bool = False
    files: list[str] = field(default_factory=list)


def load_drafts(out_dir: Path, allow_dev: bool) -> tuple[list[Draft], list[str]]:
    """통과한(drafted) 초안만. 개발용(1개 언론사)은 allow_dev 일 때만. 건너뛴 사유를 함께 돌려준다."""
    drafts, notes = [], []
    for cdir in sorted(p for p in out_dir.iterdir() if p.is_dir()):
        rpath = cdir / "result.json"
        if not rpath.exists():
            continue
        r = json.loads(rpath.read_text(encoding="utf-8"))
        if r["status"] != "drafted":
            notes.append(f"{r['cluster_id']} {r['label']}: {r['status']} — {r['reason']}")
            continue
        if r.get("dev_single_source") and not allow_dev:
            notes.append(f"{r['cluster_id']} {r['label']}: 개발용(1개 언론사) 초안이라 PR 에서 제외")
            continue
        drafts.append(Draft(r["cluster_id"], r["label"], r["slug"], (cdir / "post.md").read_text(encoding="utf-8"),
                            json.loads((cdir / "facts.json").read_text(encoding="utf-8")), r,
                            bool(r.get("dev_single_source")), bool(r.get("update_of"))))
    return drafts, notes


def unique_slug(slug: str, taken: set[str]) -> str:
    if slug not in taken:
        return slug
    n = 2
    while f"{slug}-{n}" in taken:
        n += 1
    return f"{slug}-{n}"


def stage_posts(drafts: list[Draft], root: Path, today: str) -> list[str]:
    """content/posts 에 파일을 쓰고 topics.json 을 갱신한다. 쓴 파일 경로(루트 기준)를 돌려준다."""
    posts_dir = root / "content" / "posts"
    posts_dir.mkdir(parents=True, exist_ok=True)
    taken = {p.stem for p in posts_dir.glob("*.md")}
    topics_path = root / "data" / "topics.json"
    topics = read_json(topics_path, [])
    written: list[str] = []
    for d in drafts:
        slug = d.slug
        if not d.is_update:
            slug = unique_slug(slug, taken)   # 새 글이 기존 글 주소와 겹치면 번호를 붙인다
            if slug != d.slug:
                d.markdown = re.sub(r"^slug: .*$", f"slug: {slug}", d.markdown, count=1, flags=re.M)
        taken.add(slug)
        d.slug = slug
        path = posts_dir / f"{slug}.md"
        path.write_text(d.markdown, encoding="utf-8")
        d.files = [str(path.relative_to(root))]
        written += d.files

        links = list(d.result.get("source_links", []))
        entry = next((t for t in topics if t["id"] == slug), None)
        if entry:   # 수정 초안: 출처 링크를 합친다
            entry["source_links"] = sorted(set(entry["source_links"]) | set(links))
        else:
            fm = d.markdown.split("---", 2)[1]
            pub = re.search(r"^published: '?([0-9-]+)'?", fm, re.M)
            topics.append({"id": slug, "slug": slug, "label": d.label, "created": today,
                           "published": pub.group(1) if pub else today, "source_links": sorted(set(links))})
    write_json(topics_path, topics)
    written.append("data/topics.json")
    return written


# ---------------------------------------------------------------- PR 본문
def _cell(v) -> str:
    if isinstance(v, list):
        v = "<br>".join(str(x) for x in v)
    return str(v).replace("|", "\\|").replace("\n", " ") or "—"


def fact_table(facts: dict) -> str:
    rows = []
    for k in FACT_ROWS:
        v = facts.get(k, "")
        if k == "신청기간":
            v = f"{v.get('시작') or '—'} ~ {v.get('마감') or '—'}"
        rows.append(f"| {k} | {_cell(v)} |")
    return "| 항목 | 값 |\n|---|---|\n" + "\n".join(rows)


def build_pr_body(drafts: list[Draft], today: str, run_url: str = "", notes: list[str] | None = None,
                  allow_dev: bool = False) -> str:
    L: list[str] = []
    if allow_dev:
        L += ["> **개발용 시험 PR입니다. 병합하지 마세요.** 언론사 1곳짜리 초안이 포함되어 있습니다.", ""]
    L.append(f"{today} 초안 {len(drafts)}편. 병합하면 발행됩니다.")
    if run_url:
        L.append(f"생성 실행: {run_url}")
    conflicted = [d for d in drafts if d.facts.get("기사간_불일치")]
    if conflicted:
        L += ["", "## ⚠️ 기사 간 불일치가 있는 글", ""]
        for d in conflicted:
            for c in d.facts["기사간_불일치"]:
                L.append(f"- **{d.facts['제도명']}** — {c['항목']}: {' / '.join(c['값들'])} (보도: {', '.join(c['출처']) or '미상'})")
        L.append("\n주관기관 사이트에서 확인한 뒤 병합하세요.")
    warn = [d for d in drafts if d.result.get("ungrounded_numbers") or d.result.get("removed_dates")]
    if warn:
        L += ["", "## 자동 검증 경고", ""]
        for d in warn:
            for x in d.result.get("ungrounded_numbers") or []:
                L.append(f"- **{d.facts['제도명']}** — 원문에서 찾지 못한 숫자 `{x['값']}` ({x['항목']})")
            for x in d.result.get("removed_dates") or []:
                L.append(f"- **{d.facts['제도명']}** — 근거가 없어 비운 날짜 `{x['값']}` ({x['항목']}: {x['이유']})")

    for i, d in enumerate(drafts, 1):
        sim = d.result.get("similarity") or {}
        kinds = [a["source"] for a in d.result.get("articles", [])]
        L += ["", f"## {i}. {d.facts['제도명']}", "",
              f"- 파일: `content/posts/{d.slug}.md` ({'기존 글 수정' if d.is_update else '새 글'})",
              f"- 미리보기: Cloudflare Pages 미리보기 주소의 `/post/{d.slug}/`",
              f"- 유사도: 원문 합집합 5어절 겹침 **{sim.get('union_ratio', 0) * 100:.1f}%** (기준 {sim.get('threshold', 0.1) * 100:.0f}%), "
              f"분량 {d.result.get('chars', '?')}자, 작성 시도 {len(d.result.get('attempts', []))}회",
              f"- 원문 확보: 본문 {kinds.count('body')}건 · RSS 요약 {kinds.count('rss_summary')}건"]
        if d.dev:
            L.append("- **개발용: 언론사 1곳 초안(발행 대상 아님)**")
        L += ["", "### 사실 항목", "", fact_table(d.facts)]
        missing = d.facts.get("기사에_없는_항목", [])
        L += ["", f"**기사에 없는 항목:** {', '.join(missing) if missing else '없음'}", "", "### 원문 기사", ""]
        L += [f"- [{s['언론사']}]({s['링크']}) ({s['발행일'] or '날짜 미상'})" for s in d.facts["출처"]]
    if notes:
        L += ["", "<details><summary>초안이 되지 못한 묶음</summary>", ""] + [f"- {n}" for n in notes] + ["", "</details>"]
    L += ["", "---", "", CHECKLIST]
    body = "\n".join(L)
    if len(body) > PR_BODY_LIMIT:   # 상한을 넘으면 사실 항목 표를 줄이고 체크리스트는 남긴다
        head = body[: PR_BODY_LIMIT - len(CHECKLIST) - 200]
        body = head + "\n\n_(길이 제한으로 일부 생략됨)_\n\n---\n\n" + CHECKLIST
    return body


# ---------------------------------------------------------------- git / GitHub
def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, text=True, **kw)


def gh_api(method: str, endpoint: str, payload: dict | None = None) -> dict | list:
    cmd = ["gh", "api", "-X", method, endpoint] + (["--input", "-"] if payload is not None else [])
    try:
        r = subprocess.run(cmd, input=json.dumps(payload) if payload is not None else None,
                           check=True, text=True, capture_output=True)
    except subprocess.CalledProcessError as e:   # 오류 본문(권한 부족 등)을 그대로 보여 준다
        raise RuntimeError(f"gh api {method} {endpoint} 실패: {(e.stdout or '').strip()} {(e.stderr or '').strip()}") from None
    return json.loads(r.stdout) if r.stdout.strip() else {}


def publish(branch: str, files: list[str], title: str, body: str, repo: str) -> str:
    owner = repo.split("/")[0]
    base = gh_api("GET", f"repos/{repo}")["default_branch"]
    if subprocess.run(["git", "config", "user.name"], capture_output=True, text=True).stdout.strip() == "":
        run(["git", "config", "user.name", "github-actions[bot]"])
        run(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"])
    run(["git", "checkout", "-B", branch])
    run(["git", "add", "--", *files])
    if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode == 0:
        return "변경 사항이 없어 PR 을 만들지 않았다"
    run(["git", "commit", "-q", "-m", title])
    run(["git", "push", "--force", "origin", branch])   # 봇이 소유한 초안 브랜치
    existing = gh_api("GET", f"repos/{repo}/pulls?state=open&head={owner}:{branch}")
    if existing:
        gh_api("PATCH", f"repos/{repo}/pulls/{existing[0]['number']}", {"title": title, "body": body})
        return existing[0]["html_url"] + " (갱신)"
    pr = gh_api("POST", f"repos/{repo}/pulls", {"title": title, "head": branch, "base": base, "body": body})
    return pr["html_url"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="out", help="draft.py 의 --out 폴더")
    ap.add_argument("--date", help="YYYY-MM-DD (기본: 오늘 KST)")
    ap.add_argument("--allow-dev", action="store_true", help="개발용(1개 언론사) 초안도 넣는다. PR 제목에 병합 금지를 표시한다")
    ap.add_argument("--dry-run", action="store_true", help="파일·git·API 없이 PR 본문만 출력한다")
    args = ap.parse_args()

    today = args.date or datetime.now(KST).strftime("%Y-%m-%d")
    out_dir = ROOT / args.out
    drafts, notes = load_drafts(out_dir, args.allow_dev)
    if not drafts:
        print("PR 에 올릴 초안이 없다", file=sys.stderr)
        for n in notes:
            print("  -", n, file=sys.stderr)
        return 0

    run_url = ""
    if os.environ.get("GITHUB_RUN_ID"):
        run_url = f"{os.environ['GITHUB_SERVER_URL']}/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}"

    if args.dry_run:
        for i, d in enumerate(drafts):   # 슬러그 충돌 처리는 보이게 하되 파일은 쓰지 않는다
            d.files = [f"content/posts/{d.slug}.md"]
        print(build_pr_body(drafts, today, run_url, notes, args.allow_dev))
        return 0

    files = stage_posts(drafts, ROOT, today)
    n_up = sum(1 for d in drafts if d.is_update)
    title = f"초안 {today}: {len(drafts)}편" + (f" (수정 {n_up}편)" if n_up else "")
    branch = f"drafts/{today}"
    if args.allow_dev:
        title = "[개발용 시험·병합 금지] " + title
        branch = f"drafts/dev-{today}-{datetime.now(KST):%H%M}"
    body = build_pr_body(drafts, today, run_url, notes, args.allow_dev)
    print(publish(branch, files, title, body, os.environ["GITHUB_REPOSITORY"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
