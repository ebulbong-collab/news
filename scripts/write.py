"""글 작성 — AI 2차 (지시서 3-5).

입력은 사실 JSON 뿐이다(기사 원문·링크·기사 제목은 넣지 않는다).
모델은 각 목차의 '문단 텍스트'만 쓰고, 아래는 코드가 정한다. 모델 재량에 맡기지 않기 위해서다.
  - 고정 목차 순서와 제목, 프론트매터, 참고 기사(언론사명 + 원문 링크)
  - 사실 항목이 비었을 때의 "발표 자료에서 확인되지 않았습니다"
  - 기사 간 불일치가 있을 때의 "보도마다 다르게 전해져 확인이 필요합니다"
  - 제목 형식 `{제도명} 신청방법·대상·금액 ({연도})` (지시서 §5)
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime

import yaml

from common import KST, ROOT
from extract import is_empty
from llm import call_structured

MISSING = "발표 자료에서 확인되지 않았습니다"
CONFLICT = "보도마다 다르게 전해져 확인이 필요합니다"
BANNED = ["놓치면 손해", "놓치지 마", "최대 수백만", "절대", "무조건", "역대급", "대박"]
SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+){1,7}$")

_STR = {"type": "string"}
WRITE_SCHEMA = {
    "type": "object",
    "properties": {
        "slug": _STR, "summary": _STR, "target": _STR, "amount": _STR, "period": _STR, "howto": _STR,
        "faq": {"type": "array", "items": {
            "type": "object", "properties": {"q": _STR, "a": _STR},
            "required": ["q", "a"], "additionalProperties": False}},
        "contact": _STR,
    },
    "required": ["slug", "summary", "target", "amount", "period", "howto", "faq", "contact"],
    "additionalProperties": False,
}

WRITER_KEYS = ["제도명", "주관기관", "지역", "지역명", "시도", "분야", "대상", "금액", "신청기간",
               "신청방법", "필요서류", "문의처", "시행일", "기사에_없는_항목"]


@dataclass
class Post:
    markdown: str
    prose: str                    # 유사도·분량 판정에 쓰는 본문(제목·참고 기사 제외)
    chars: int
    slug: str
    meta: dict
    problems: list[str] = field(default_factory=list)


def writer_input(facts: dict, feedback: str | None = None) -> str:
    """글쓰기 단계에 넘기는 입력. 출처(언론사·제목·링크)와 skip 정보는 넣지 않는다."""
    data = {k: facts[k] for k in WRITER_KEYS}
    data["기사간_불일치"] = [{"항목": d["항목"], "값들": d["값들"]} for d in facts["기사간_불일치"]]
    s = json.dumps(data, ensure_ascii=False, indent=1)
    return s + (f"\n\n재작성 요청: {feedback}" if feedback else "")


def section_of(item: str) -> str:
    """불일치 항목명이 어느 목차에 해당하는지."""
    if re.search("대상", item):
        return "target"
    if re.search("금액|지급액|인하율|감면|한도|지원액", item):
        return "amount"
    if re.search("기간|마감|시작|시행|일정|날짜|종료", item):
        return "period"
    if re.search("방법|서류|신청", item):
        return "howto"
    return "summary"


def _valid_date(s: str) -> str:
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return s
    except (TypeError, ValueError):
        return ""


def build_post(facts: dict, w: dict, today: datetime | None = None) -> Post:
    today = today or datetime.now(KST)
    day = today.strftime("%Y-%m-%d")
    conflicts: dict[str, list[dict]] = {}
    for d in facts["기사간_불일치"]:
        conflicts.setdefault(section_of(d["항목"]), []).append(d)

    def pick(sec: str, present: bool) -> str:
        # 값이 갈리는 항목은 '없는 항목'이 아니므로 모델이 쓴 문단을 살린다.
        if not present and sec not in conflicts:
            text = MISSING + "."
        else:
            text = w[sec].strip()
        for d in conflicts.get(sec, []):
            text += f"\n\n※ {d['항목']}: {CONFLICT}. (보도된 값: {', '.join(d['값들'])})"
        return text

    has = lambda k: not is_empty(facts, k)  # noqa: E731
    sec = {
        "summary": pick("summary", True),
        "target": pick("target", has("대상")),
        "amount": pick("amount", has("금액")),
        "period": pick("period", has("신청기간") or has("시행일")),
        "howto": pick("howto", has("신청방법") or has("필요서류")),
        "contact": pick("contact", has("문의처")),
    }
    faq = [(x["q"].strip(), x["a"].strip()) for x in w["faq"]]

    deadline = _valid_date(facts["신청기간"].get("마감", ""))
    year = (deadline or day)[:4]
    slug = w["slug"].strip().lower()
    problems: list[str] = []
    if not SLUG_RE.match(slug):
        slug = "policy-" + hashlib.sha1(facts["제도명"].encode()).hexdigest()[:8]
        problems.append("slug 형식이 맞지 않아 대체했다")

    meta = {
        "title": f"{facts['제도명']} 신청방법·대상·금액 ({year})",
        "slug": slug,
        "region": facts["시도"],
        "topic": facts["분야"],
        "deadline": deadline,
        "published": day,
        "updated": day,
        "sources": [{"name": s["언론사"], "url": s["링크"], "date": s["발행일"]} for s in facts["출처"]],
    }

    faq_md = "\n\n".join(f"### Q{i}. {q}\n\n{a}" for i, (q, a) in enumerate(faq, 1))
    refs = "\n".join(f"- [{s['언론사']}]({s['링크']}) · {s['발행일'] or '날짜 미상'}" for s in facts["출처"])
    body = (
        f"## 한 줄 요약\n\n{sec['summary']}\n\n"
        f"## 누가 받을 수 있나\n\n{sec['target']}\n\n"
        f"## 얼마나 받나\n\n{sec['amount']}\n\n"
        f"## 언제까지 신청하나\n\n{sec['period']}\n\n"
        f"## 어떻게 신청하나\n\n{sec['howto']}\n\n"
        f"## 자주 묻는 질문\n\n{faq_md}\n\n"
        f"## 문의처\n\n{sec['contact']}\n\n"
        f"## 참고 기사\n\n{refs}\n"
    )
    front = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False, width=1000).strip()
    markdown = f"---\n{front}\n---\n\n{body}"

    prose = "\n".join([sec["summary"], sec["target"], sec["amount"], sec["period"], sec["howto"],
                       *[f"{q}\n{a}" for q, a in faq], sec["contact"]])
    chars = len(re.sub(r"\s+", " ", prose).strip())

    if len(faq) != 3 or any(not q or not a for q, a in faq):
        problems.append(f"FAQ 가 정확히 3개의 질문·답이 아니다(현재 {len(faq)}개)")
    if any(not v.strip() for v in sec.values()):
        problems.append("비어 있는 목차가 있다")
    hit = [b for b in BANNED if b in prose]
    if hit:
        problems.append(f"과장 표현 사용: {', '.join(hit)}")
    return Post(markdown, prose, chars, slug, meta, problems)


def write_post(facts: dict, cfg: dict, feedback: str | None = None) -> tuple[dict, dict]:
    d = cfg["draft"]
    return call_structured(
        cfg, model=cfg["models"]["write"],
        system=(ROOT / "prompts" / "write.md").read_text(encoding="utf-8"),
        user=writer_input(facts, feedback), schema=WRITE_SCHEMA,
        effort=d["write_effort"], max_tokens=d["write_max_tokens"], fallbacks=d["write_fallbacks"])
