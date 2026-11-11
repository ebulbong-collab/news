"""사실 추출 — AI 1차 (지시서 3-4).

묶음의 기사 본문들을 넣고 사실 JSON 만 받는다. 이후 단계(글 작성)에는 이 JSON 만 넘어간다.
링크·언론사 같은 출처 정보는 모델이 아니라 코드가 채운다(링크를 지어내지 못하게).
"""
from __future__ import annotations

import re
from datetime import datetime

from common import KST, ROOT
from fetch_body import Article
from llm import call_structured

SIDO = ["전국", "서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종", "경기", "강원",
        "충북", "충남", "전북", "전남", "경북", "경남", "제주", ""]
FIELDS = ["주관기관", "대상", "금액", "신청기간", "신청방법", "필요서류", "문의처", "시행일"]
CORE = ["대상", "금액", "신청기간"]   # 지시서 §4 씬 콘텐츠 판정 대상

_STR = {"type": "string"}
_STRS = {"type": "array", "items": {"type": "string"}}

EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "skip": {"type": "boolean"},
        "이유": _STR,
        "제도명": _STR,
        "주관기관": _STR,
        "지역": {"type": "string", "enum": ["전국", "시도", "시군구", ""]},
        "지역명": _STR,
        "시도": {"type": "string", "enum": SIDO},
        "분야": {"type": "string", "enum": ["지원금", "바우처", "환급", "요금", "세금", "제도", ""]},
        "대상": _STRS,
        "금액": _STRS,
        "신청기간": {"type": "object",
                 "properties": {"시작": _STR, "마감": _STR},
                 "required": ["시작", "마감"], "additionalProperties": False},
        "신청방법": _STRS,
        "필요서류": _STRS,
        "문의처": _STR,
        "시행일": _STR,
        "기사간_불일치": {"type": "array", "items": {
            "type": "object",
            "properties": {"항목": _STR, "값들": _STRS, "출처": _STRS},
            "required": ["항목", "값들", "출처"], "additionalProperties": False}},
        "기사에_없는_항목": _STRS,
    },
    "required": ["skip", "이유", "제도명", "주관기관", "지역", "지역명", "시도", "분야", "대상", "금액",
                 "신청기간", "신청방법", "필요서류", "문의처", "시행일", "기사간_불일치", "기사에_없는_항목"],
    "additionalProperties": False,
}


def render_articles(arts: list[Article]) -> str:
    today = datetime.now(KST).strftime("%Y-%m-%d")
    parts = [f"오늘 날짜: {today}\n"]
    n = 0
    for a in arts:
        if a.source == "none":
            continue
        n += 1
        kind = "본문" if a.source == "body" else "RSS 요약"
        parts.append(f'<기사 번호="{n}" 언론사="{a.publisher}" 발행일="{a.date() or "미상"}" 자료유형="{kind}">\n'
                     f"제목: {a.title}\n{a.text}\n</기사>")
    return "\n\n".join(parts)


def is_empty(facts: dict, key: str) -> bool:
    v = facts.get(key)
    if key == "신청기간":
        return not (v and (v.get("시작") or v.get("마감")))
    return not v


def finalize_facts(raw: dict, arts: list[Article]) -> dict:
    """모델 출력에 코드가 채우는 값(출처)과 검증을 더한다."""
    f = dict(raw)
    f["출처"] = [{"언론사": a.publisher, "제목": a.title, "링크": a.link, "발행일": a.date()}
               for a in arts if a.source != "none"]
    pubs = {a.publisher for a in arts}
    f["기사간_불일치"] = [
        {**d, "출처": [p for p in d["출처"] if p in pubs]}
        for d in raw["기사간_불일치"] if len(set(d["값들"])) >= 2]
    missing = {k for k in raw["기사에_없는_항목"] if k in FIELDS}
    # 불일치로 적힌 항목은 '없는 항목'이 아니라 '값이 갈리는 항목'이다.
    conflicted = {d["항목"] for d in f["기사간_불일치"]}
    for k in FIELDS:
        if is_empty(f, k) and k not in conflicted:
            missing.add(k)
    f["기사에_없는_항목"] = [k for k in FIELDS if k in missing]
    return f


def is_thin(facts: dict, limit: int) -> bool:
    """대상·금액·기간 중 기사에 없는 항목이 limit 개 이상이면 초안을 만들지 않는다(지시서 §4)."""
    conflicted = {d["항목"] for d in facts["기사간_불일치"]}
    return sum(1 for k in CORE if k in facts["기사에_없는_항목"] and k not in conflicted) >= limit


_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _norm_num(tok: str) -> str:
    tok = tok.replace(",", "").rstrip(".")
    whole, _, frac = tok.partition(".")
    whole = whole.lstrip("0") or "0"
    return whole + ("." + frac if frac else "")


def ground_check(facts: dict, arts: list[Article]) -> list[dict]:
    """사실 값에 적힌 숫자가 원문(본문·제목)에 실제로 있는지 대조한다. 없으면 검수자 경고 대상.

    - 연도(20xx)는 발행일 기준으로 모델이 채우는 값이라 제외한다.
    - 원문에 숫자가 없다고 틀렸다는 뜻은 아니다(예: '내년'→연도). 검수자가 확인할 후보를 줄이는 용도다.
    """
    corpus = {_norm_num(t) for a in arts for t in _NUM.findall(a.text + " " + a.title)}
    texts: list[tuple[str, str]] = []
    for k in ("제도명", "주관기관", "문의처", "시행일"):
        texts.append((k, facts.get(k, "")))
    for k in ("대상", "금액", "신청방법", "필요서류"):
        texts += [(k, s) for s in facts.get(k, [])]
    period = facts.get("신청기간", {})
    texts += [("신청기간", period.get("시작", "")), ("신청기간", period.get("마감", ""))]
    out, seen = [], set()
    for field, s in texts:
        for tok in _NUM.findall(s):
            if re.fullmatch(r"20\d\d", tok):
                continue
            if _norm_num(tok) not in corpus and (field, tok) not in seen:
                seen.add((field, tok))
                out.append({"항목": field, "값": tok})
    return out


def extract_facts(arts: list[Article], cfg: dict) -> tuple[dict, dict]:
    d = cfg["draft"]
    raw, usage = call_structured(
        cfg, model=cfg["models"]["cluster_extract"],
        system=(ROOT / "prompts" / "extract.md").read_text(encoding="utf-8"),
        user=render_articles(arts), schema=EXTRACT_SCHEMA,
        effort=d["extract_effort"], max_tokens=d["extract_max_tokens"])
    return finalize_facts(raw, arts), usage
