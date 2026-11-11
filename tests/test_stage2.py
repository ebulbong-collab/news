"""2단계(본문·추출·작성·유사도·파이프라인) 오프라인 테스트. API·네트워크를 쓰지 않는다.

실행: python tests/test_stage2.py
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import draft  # noqa: E402
import extract  # noqa: E402
import fetch_body  # noqa: E402
import similarity  # noqa: E402
import write  # noqa: E402
from common import load_config  # noqa: E402
from fetch_body import Article  # noqa: E402

SRC = ("정부가 이달 말 종료 예정인 유류세 인하를 2개월 연장한다 재정경제부는 11일 비상경제장관회의에서 "
       "휘발유 15퍼센트 경유와 액화석유가스 부탄은 25퍼센트인 현행 인하율을 유지하기로 했다 "
       "이번 연장으로 인하 조치는 내년 1월 31일까지 이어진다")

FACTS = {
    "skip": False, "이유": "", "제도명": "유류세 한시 인하", "주관기관": "재정경제부",
    "지역": "전국", "지역명": "전국", "시도": "전국", "분야": "세금",
    "대상": ["휘발유·경유·LPG 부탄을 쓰는 소비자"], "금액": ["휘발유 15%", "경유 25%", "LPG 부탄 25%"],
    "신청기간": {"시작": "", "마감": "2027-01-31"}, "신청방법": [], "필요서류": [], "문의처": "", "시행일": "",
    "기사간_불일치": [], "기사에_없는_항목": [],
}


def arts():
    return [Article("경향신문", "유류세 인하 연장", "https://k.example/1", "2026-11-11T10:14+09:00", "body", SRC, "allowed"),
            Article("뉴시스", "유류세 인하 2개월 연장", "https://n.example/1", "2026-11-11T09:00+09:00", "body", SRC + " 추가 문장입니다", "allowed")]


def filler(n):
    return " ".join(f"안내 문장 {i}번은 서로 다른 단어 {i * 7}과 {i * 13}을 넣어 길이를 채웁니다." for i in range(n))


def writer_out(copy_source: bool, n_filler: int = 14) -> dict:
    lead = SRC if copy_source else "정부가 유류세 인하 조치를 두 달 더 이어 가기로 정했습니다."
    return {"slug": "fuel-tax-cut-extension-2027", "summary": lead,
            "target": ("휘발유 경유 부탄 사용자 모두가 해당합니다. " + (SRC[:90] if copy_source else "")),
            "amount": "휘발유 15%, 경유 25%, LPG 부탄 25%의 인하율이 그대로 적용됩니다. " + filler(n_filler),
            "period": "적용은 2027-01-31까지 이어집니다.", "howto": "별도 신청 절차는 확인되지 않았습니다.",
            "faq": [{"q": f"질문 {i}", "a": f"답변 {i}. " + filler(2)} for i in range(1, 4)],
            "contact": "문의처는 확인되지 않았습니다."}


def test_similarity():
    assert similarity.overlap_ratio(SRC, SRC) == 1.0
    assert similarity.overlap_ratio("전혀 다른 내용의 짧지 않은 한국어 문장 하나", SRC) == 0.0
    r = similarity.check(SRC[:60] + " " + filler(10), {"a": SRC}, 0.10)
    assert r["union_ratio"] > 0 and "a" in r["per_article"]
    assert similarity.flagged_sentences(SRC + ". 다른 문장입니다 완전히 다른 단어들로 구성된.", {"a": SRC})


def test_extract_helpers():
    f = extract.finalize_facts({**FACTS, "기사간_불일치": [{"항목": "금액", "값들": ["A", "A"], "출처": ["경향신문", "없는사"]}],
                                "기사에_없는_항목": ["문의처", "엉뚱한항목"]}, arts())
    assert f["기사간_불일치"] == []                     # 값이 1종류면 불일치가 아니다
    assert "엉뚱한항목" not in f["기사에_없는_항목"] and "문의처" in f["기사에_없는_항목"]
    assert [s["언론사"] for s in f["출처"]] == ["경향신문", "뉴시스"]   # 출처는 코드가 채운다
    assert extract.is_thin({**f, "기사에_없는_항목": ["대상", "금액"]}, 2)
    assert not extract.is_thin({**f, "기사에_없는_항목": ["대상"]}, 2)
    assert extract.ground_check(f, arts()) == []         # 15, 25, 31, 1 은 원문에 있다
    bad = extract.ground_check({**f, "금액": ["휘발유 99%"]}, arts())
    assert bad == [{"항목": "금액", "값": "99"}], bad


def test_build_post():
    f = extract.finalize_facts(FACTS, arts())
    p = write.build_post(f, writer_out(False))
    assert p.meta["title"] == "유류세 한시 인하 신청방법·대상·금액 (2027)" and p.meta["deadline"] == "2027-01-31"
    heads = [l for l in p.markdown.splitlines() if l.startswith("## ")]
    assert heads == ["## 한 줄 요약", "## 누가 받을 수 있나", "## 얼마나 받나", "## 언제까지 신청하나",
                     "## 어떻게 신청하나", "## 자주 묻는 질문", "## 문의처", "## 참고 기사"], heads
    assert "[경향신문](https://k.example/1)" in p.markdown            # 참고 기사: 언론사명 + 원문 링크
    assert write.MISSING in p.markdown.split("## 문의처")[1]          # 빈 문의처는 코드가 표준 문구로 대체
    assert write.MISSING in p.markdown.split("## 어떻게 신청하나")[1].split("##")[0]
    assert p.markdown.startswith("---\n") and not p.problems
    # 불일치는 표준 문구가 코드로 덧붙는다
    f2 = {**f, "기사간_불일치": [{"항목": "신청 마감일", "값들": ["2027-01-31", "2027-02-28"], "출처": ["경향신문"]}]}
    p2 = write.build_post(f2, writer_out(False))
    assert write.CONFLICT in p2.markdown and "2027-02-28" in p2.markdown
    # 구성 문제 탐지
    w = writer_out(False); w["faq"] = w["faq"][:2]; w["slug"] = "한글 슬러그"; w["summary"] += " 놓치면 손해입니다"
    p3 = write.build_post(f, w)
    assert len(p3.problems) == 3 and p3.slug.startswith("policy-"), p3.problems
    # 글쓰기 입력에는 출처(언론사·제목·링크)가 들어가지 않는다
    wi = write.writer_input(f2)
    assert "k.example" not in wi and "유류세 인하 연장" not in wi and "출처" not in wi


def test_fetch_helpers():
    pub = [{"publisher": "A", "pub": "2026-11-11T09:00"}, {"publisher": "A", "pub": "2026-11-11T10:00"},
           {"publisher": "B", "pub": "2026-11-11T08:00"}, {"publisher": "C", "pub": None}]
    got = fetch_body.pick_articles(pub, 3)
    assert {a["publisher"] for a in got} == {"A", "B", "C"}
    html = ("<html><body><nav>메뉴 로그인</nav><article><h1>제목</h1>" +
            "".join(f"<p>{SRC} 문단 {i}입니다. 저작권자 ⓒ 무단 전재 금지</p><p>{SRC} 다른 문단 {i}</p>" for i in range(4)) +
            "</article></body></html>").encode()
    text = fetch_body.extract_body(html, 4000)
    assert "유류세 인하" in text and "무단" not in text and len(text) <= 4000, text[:80]
    # Arc XP(조선일보 등): 본문이 HTML 안의 Fusion.globalContent JSON 에 있다
    fusion = ('<html><script>Fusion.globalContent={"content_elements":[{"type":"text","content":"<b>첫 문단</b> ' + SRC + '"},'
              '{"type":"image","content":"x"},{"type":"text","content":"둘째 문단 ' + SRC + '"}]};Fusion.x=1;</script></html>')
    got = fetch_body.extract_body(fusion.encode(), 4000, 100)
    assert got.count("\n") == 1 and got.startswith("첫 문단") and "image" not in got, got[:60]


def test_pipeline_with_rewrite():
    cfg = load_config()
    calls = {"write": 0}

    def fake_llm(cfg_, *, model, system, user, schema, effort, max_tokens, fallbacks=False):
        usage = {"model": model, "served_model": model, "request_id": "req_test", "input_tokens": 1000, "output_tokens": 500}
        if schema is extract.EXTRACT_SCHEMA:
            assert "<기사 번호=" in user and "경향신문" in user
            return dict(FACTS), usage
        calls["write"] += 1
        # 첫 시도는 원문을 베낀 초안(유사도 초과), 재작성에서 새로 쓴 초안
        if calls["write"] == 1:
            assert "재작성 요청" not in user
            return writer_out(True, 4), usage
        assert "재작성 요청" in user and "원문과 5어절 겹침" in user, user[-400:]
        assert "비상경제장관회의" not in user.split("재작성 요청")[0]   # 글쓰기 입력에 원문이 없다
        return writer_out(False), usage

    extract.call_structured = fake_llm
    write.call_structured = fake_llm
    draft.fetch_articles = lambda picked, cfg_: arts()
    cluster = {"cluster_id": "c01", "label": "유류세", "eligible": True, "policy_related": True,
               "action": "new", "articles": [{"id": "1", "publisher": "경향신문", "title": "t", "link": "l", "pub": None}]}
    with tempfile.TemporaryDirectory() as tmp:
        res = draft.process_cluster(cluster, cfg, False, Path(tmp))
        assert res["status"] == "drafted", res
        assert len(res["attempts"]) == 2 and res["attempts"][0]["similarity"] > 0.10 >= res["attempts"][1]["similarity"], res["attempts"]
        assert 800 <= res["chars"] <= 1500, res["chars"]
        assert (Path(tmp) / "c01" / "post.md").exists() and (Path(tmp) / "c01" / "facts.json").exists()
        saved = json.dumps(res, ensure_ascii=False) + (Path(tmp) / "c01" / "facts.json").read_text(encoding="utf-8")
        assert "비상경제장관회의" not in saved                 # 원문 문장이 결과 파일에 남지 않는다
        assert res["cost_usd_est"] > 0
        # 계속 베끼면 폐기된다
        calls["write"] = 0
        extract.call_structured = write.call_structured = lambda *a, **k: (
            dict(FACTS) if k["schema"] is extract.EXTRACT_SCHEMA else writer_out(True, 4),
            {"model": k["model"], "served_model": k["model"], "request_id": "r", "input_tokens": 1, "output_tokens": 1})
        res2 = draft.process_cluster({**cluster, "cluster_id": "c02"}, cfg, False, Path(tmp))
        assert res2["status"] == "discarded" and "similarity" in res2["reason"], res2
        assert (Path(tmp) / "c02" / "rejected.md").exists() and not (Path(tmp) / "c02" / "post.md").exists()
        # 언론사 1곳뿐이면(개발용 플래그 없이) 건너뛴다
        draft.fetch_articles = lambda picked, cfg_: arts()[:1]
        res3 = draft.process_cluster({**cluster, "cluster_id": "c03"}, cfg, False, Path(tmp))
        assert res3["status"] == "skipped" and "insufficient_sources" in res3["reason"], res3


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
