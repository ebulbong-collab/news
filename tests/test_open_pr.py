"""초안 PR 생성 오프라인 테스트(git·GitHub 호출 없음). 실행: python tests/test_open_pr.py"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import open_pr  # noqa: E402

POST = """---
title: 유류세 한시 인하 대상·금액·적용기간 (2027)
slug: fuel-tax-cut
region: 전국
topic: 세금
deadline: '2027-01-31'
published: '2026-11-11'
updated: '2026-11-11'
sources:
- name: 경향신문
  url: https://k.example/1
  date: '2026-11-11'
---

## 한 줄 요약

요약입니다.
"""

FACTS = {"제도명": "유류세 한시 인하", "주관기관": "재정경제부", "지역명": "전국", "분야": "세금", "대상": ["유류 사용자"],
         "금액": ["휘발유 15%", "경유 25%"], "신청기간": {"시작": "", "마감": "2027-01-31"}, "신청방법": [], "필요서류": [],
         "문의처": "", "시행일": "", "기사에_없는_항목": ["신청방법", "필요서류", "문의처", "시행일"],
         "기사간_불일치": [], "출처": [{"언론사": "경향신문", "제목": "t", "링크": "https://k.example/1", "발행일": "2026-11-11"},
                                   {"언론사": "뉴시스", "제목": "t", "링크": "https://n.example/1", "발행일": "2026-11-11"}]}


def result(cid, status="drafted", dev=False, slug="fuel-tax-cut", **kw):
    return {"cluster_id": cid, "label": "유류세", "status": status, "reason": kw.pop("reason", ""), "dev_single_source": dev, "slug": slug,
            "chars": 691, "similarity": {"union_ratio": 0.012, "threshold": 0.1}, "attempts": [{}, {}],
            "articles": [{"source": "body"}, {"source": "rss_summary"}], "source_links": ["https://k.example/1?utm_source=a", "https://n.example/1"],
            "update_of": kw.pop("update_of", None), **kw}


def write_cluster(out: Path, name: str, res: dict, post: str | None = POST, facts: dict | None = None):
    d = out / name
    d.mkdir(parents=True)
    (d / "result.json").write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
    (d / "facts.json").write_text(json.dumps(facts or FACTS, ensure_ascii=False), encoding="utf-8")
    if post:
        (d / "post.md").write_text(post, encoding="utf-8")


def test_load_drafts_filters():
    with tempfile.TemporaryDirectory() as t:
        out = Path(t)
        write_cluster(out, "c01", result("c01"))
        write_cluster(out, "DEV-c03", result("c03", dev=True, slug="dev-one"))                    # 개발용(1개 언론사)
        write_cluster(out, "c04", result("c04", status="skipped", reason="irrelevant"), post=None)  # 건너뜀
        ds, notes = open_pr.load_drafts(out, allow_dev=False)
        assert [d.cluster_id for d in ds] == ["c01"] and len(notes) == 2                         # 개발용은 PR 에서 제외
        assert sorted(d.cluster_id for d in open_pr.load_drafts(out, allow_dev=True)[0]) == ["c01", "c03"]


def test_stage_posts_and_topics():
    with tempfile.TemporaryDirectory() as t, tempfile.TemporaryDirectory() as o:
        root, out = Path(t), Path(o)
        (root / "content" / "posts").mkdir(parents=True)
        (root / "content" / "posts" / "fuel-tax-cut.md").write_text("기존 글", encoding="utf-8")   # 같은 슬러그의 다른 주제
        write_cluster(out, "c01", result("c01"))
        ds, _ = open_pr.load_drafts(out, False)
        files = open_pr.stage_posts(ds, root, "2026-11-11")
        # 기존 글을 덮어쓰지 않고 번호를 붙인다. 프론트매터의 slug 도 바뀐다.
        assert ds[0].slug == "fuel-tax-cut-2" and "slug: fuel-tax-cut-2" in (root / "content/posts/fuel-tax-cut-2.md").read_text(encoding="utf-8")
        assert (root / "content/posts/fuel-tax-cut.md").read_text(encoding="utf-8") == "기존 글"
        assert files == ["content/posts/fuel-tax-cut-2.md", "data/topics.json"]
        topics = json.loads((root / "data/topics.json").read_text(encoding="utf-8"))
        assert topics[0]["id"] == "fuel-tax-cut-2" and topics[0]["published"] == "2026-11-11"
        assert "https://k.example/1?utm_source=a" in topics[0]["source_links"]      # 묶음 비교용 원래 링크

        # 수정 초안: 같은 슬러그를 덮어쓰고 출처 링크를 합치며 topics 항목을 늘리지 않는다
        out2 = Path(o) / "second"
        write_cluster(out2, "c09", result("c09", slug="fuel-tax-cut-2", update_of="fuel-tax-cut-2", source_links=["https://h.example/9"]), post=POST.replace("요약입니다", "수정입니다"))
        ds2, _ = open_pr.load_drafts(out2, False)
        open_pr.stage_posts(ds2, root, "2026-11-12")
        assert "수정입니다" in (root / "content/posts/fuel-tax-cut-2.md").read_text(encoding="utf-8")
        topics = json.loads((root / "data/topics.json").read_text(encoding="utf-8"))
        assert len(topics) == 1 and "https://h.example/9" in topics[0]["source_links"] and len(topics[0]["source_links"]) == 3


def test_pr_body():
    with tempfile.TemporaryDirectory() as t:
        out = Path(t)
        conflict = {**FACTS, "기사간_불일치": [{"항목": "신청 마감일", "값들": ["2027-01-31", "2027-02-28"], "출처": ["경향신문", "뉴시스"]}]}
        write_cluster(out, "c01", result("c01", ungrounded_numbers=[{"항목": "금액", "값": "99"}],
                                         removed_dates=[{"항목": "시행일", "값": "2026-12-01", "이유": "원문에서 확인되지 않음"}]), facts=conflict)
        write_cluster(out, "c02", result("c02", slug="other-one"))
        ds, notes = open_pr.load_drafts(out, False)
        body = open_pr.build_pr_body(ds, "2026-11-11", "https://run", notes)
        assert body.index("기사 간 불일치") < body.index("## 1.")                    # 불일치 경고는 맨 위
        assert "신청 마감일: 2027-01-31 / 2027-02-28" in body
        assert "원문에서 찾지 못한 숫자 `99`" in body and "근거가 없어 비운 날짜 `2026-12-01`" in body
        assert "| 금액 | 휘발유 15%<br>경유 25% |" in body and "2027-01-31" in body       # 사실 항목 표
        assert "기사에 없는 항목:** 신청방법, 필요서류, 문의처, 시행일" in body
        assert "[경향신문](https://k.example/1)" in body and "1.2%" in body              # 원문 링크, 유사도 수치
        assert body.rstrip().endswith("자동 병합은 설정하지 않았습니다.") and "### 검수 체크리스트" in body
        assert body.count("- [ ] ") == 5
        assert "병합하지 마세요" not in body and "병합하지 마세요" in open_pr.build_pr_body(ds, "d", allow_dev=True)
        # 본문이 상한을 넘으면 줄이되 체크리스트는 남긴다
        big = open_pr.build_pr_body(ds * 40, "2026-11-11")
        assert len(big) <= open_pr.PR_BODY_LIMIT + 600 and "### 검수 체크리스트" in big


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
