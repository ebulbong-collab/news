"""유사도 검사 (지시서 3-6): 어절 5개 단위 겹침 비율.

초안의 5어절 묶음(shingle) 중 원문 기사에도 나오는 비율을 센다. 임계값(기본 10%)을 넘으면 탈락이다.
탈락 판정은 '모든 원문 합집합' 기준(가장 엄격)이며, 기사별 비율도 함께 보고한다.
"""
from __future__ import annotations

import re

_STRIP = re.compile(r"[^\w가-힣]+", re.UNICODE)
_SENT = re.compile(r"(?<=[.!?。])\s+|\n+")


def tokens(text: str) -> list[str]:
    """어절 = 공백 단위. 문장부호·기호는 지우고 소문자로 맞춘다."""
    out = []
    for w in text.split():
        w = _STRIP.sub("", w).lower()
        if w:
            out.append(w)
    return out


def shingles(text: str, n: int = 5) -> set[tuple[str, ...]]:
    t = tokens(text)
    return {tuple(t[i:i + n]) for i in range(len(t) - n + 1)}


def overlap_ratio(draft: str, source: str, n: int = 5) -> float:
    d = shingles(draft, n)
    return len(d & shingles(source, n)) / len(d) if d else 0.0


def check(draft_prose: str, sources: dict[str, str], threshold: float, n: int = 5) -> dict:
    """sources: {언론사명(또는 식별자): 원문 텍스트}. 반환 dict 에는 원문 텍스트를 넣지 않는다."""
    d = shingles(draft_prose, n)
    per = {k: (len(d & shingles(v, n)) / len(d) if d else 0.0) for k, v in sources.items()}
    union = set().union(*(shingles(v, n) for v in sources.values())) if sources else set()
    union_ratio = len(d & union) / len(d) if d else 0.0
    return {
        "n": n, "draft_shingles": len(d), "threshold": threshold,
        "union_ratio": round(union_ratio, 4),
        "per_article": {k: round(v, 4) for k, v in per.items()},
        "passed": union_ratio <= threshold,
    }


def flagged_sentences(draft_prose: str, sources: dict[str, str], n: int = 5, limit: int = 8) -> list[str]:
    """원문과 5어절 이상 겹친 '초안의' 문장. 재작성 지시에 쓴다(원문 텍스트는 돌려주지 않는다)."""
    union = set().union(*(shingles(v, n) for v in sources.values())) if sources else set()
    flagged = []
    for s in _SENT.split(draft_prose):
        s = s.strip()
        if s and shingles(s, n) & union:
            flagged.append(s)
    return flagged[:limit]
