"""키워드 필터·제목 정리 회귀 테스트. 실행: python tests/test_keywords.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from common import Keywords, clean_title  # noqa: E402

# (제목, 요약, 통과 기대, 이유)
CASES = [
    # 통과해야 하는 것
    ("하남시, 정신건강 심리상담 바우처사업 13일부터 재개", "", True),
    ("유류세 인하 내년 1월까지 2개월 연장", "", True),
    ("내년부터 위기가구에 생계비 30만원 우선지급, 낙상노인 치료비도 지원", "", True),
    ("경남 남해, 기본소득 시행 후 마을상점 활성화", "", True),
    ("상속공제 개편의 역설", "", True),
    ("부안군, 어업용 면세유 지원… 410어가 최대 300만원", "어업용 면세유 지원금 지급", True),
    # 0단계에서 확인된 오탐 — 걸러져야 한다
    ("신현송 한국은행 총재 BIS 지급 및 시장인프라위 의장 선임", "", False),
    ("인하대병원 노사, 직장내 괴롭힘·갑질 근절 공동선언", "", False),
    ("美 금리 인상 공포에 커지는 우려", "", False),
    ("볼보·폴스타, 韓서 '중국산' 연 1만대 판매 눈앞…\"가격 인하에 신차 가세\"", "", False),
    ("당국 미지급 보상금에 앙심… 中 40대 굴착기 몰고 청사 돌진", "", False),
    ("정읍 시내버스 내년 1월 노선 개편… 통학 노선 신설", "", False),
    ("경주시, 미국 현지서 기업 투자 유치전 펼쳐", "보조금 …", False),
    ("코스피 반등마다 1조원 넘게 파는 개미들", "", False),
]


def main() -> int:
    kw = Keywords()
    bad = 0
    for title, desc, expected in CASES:
        got = kw.match(title, desc).passed
        if got != expected:
            bad += 1
            print(f"FAIL expected={expected} got={got}: {title}")
    assert clean_title('quot;석달째 임금 못 받아quot;hellip;홈플러스') == '"석달째 임금 못 받아"…홈플러스'
    assert clean_title("직원들 &amp;quot;R&amp;amp;D&amp;quot; 존중") == '직원들 "R&D" 존중'
    assert clean_title("눈 내리기 1&#8764;2시간 전") == "눈 내리기 1∼2시간 전"
    print(f"{len(CASES) - bad}/{len(CASES)} keyword cases ok, title cleaning ok")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
