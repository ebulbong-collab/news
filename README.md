# 지원금소식(가칭)

여러 언론사 기사에서 지원금·제도 변경 소식의 **사실만** 뽑아 대상·금액·기간·신청방법으로 정리하는 정적 사이트와, 이를 만드는 자동화 파이프라인입니다. 사람이 승인한 글만 발행됩니다.

```
RSS 수집 → 키워드 필터 → 주제 묶기 → 본문 가져오기 → 사실 추출(AI) → 글 작성(AI) → 유사도 검사 → 초안 PR → 사람 검수 → 병합 = 발행
```

## 구성

| 경로 | 역할 |
|---|---|
| `config.yaml` | 사이트명·도메인, 모델, 임계값. 도메인은 지금 placeholder |
| `feeds.yaml`, `keywords.yaml` | RSS 목록, 키워드 필터 |
| `scripts/` | `collect` `cluster` `fetch_body` `extract` `write` `similarity` `draft` `open_pr` `build` |
| `prompts/` | 묶기·추출·작성 프롬프트 |
| `content/posts/` | 발행된 글(마크다운). **PR 병합 = 발행** |
| `content/guide/`, `content/pages/` | 가이드, 소개·개인정보처리방침 |
| `templates/`, `static/` | 사이트 템플릿과 CSS |
| `data/` | 수집 이력·후보·묶음 결과(메타데이터만. 기사 본문은 저장하지 않음) |

## 워크플로

| 워크플로 | 시점 | 하는 일 |
|---|---|---|
| `collect` | 하루 3회(KST 07·12·18시) | 수집·필터. 18시 실행은 주제 묶기까지 |
| `draft` | 매일 KST 18:30 (**변수로 켠 뒤**), 수동 | 초안 생성 → 초안 PR |
| `daily-build` | 매일 KST 00:10 | Cloudflare Pages 재빌드(마감 상태 갱신) |
| `tests` | 코드 변경 시 | 오프라인 테스트와 사이트 빌드 확인 |
| `check-feeds` | 수동 | 피드 상태 점검 |

## 사이트 빌드

```bash
pip install -r requirements.txt
python scripts/build.py          # dist/ 에 생성
```

Cloudflare Pages: Build command `python scripts/build.py`, Output directory `dist`, 환경변수 `PYTHON_VERSION=3.12`.

## 설정해야 하는 것

- GitHub Secrets: `ANTHROPIC_API_KEY`, (선택) `CF_PAGES_DEPLOY_HOOK`
- GitHub Settings → Actions → General: "Allow GitHub Actions to create and approve pull requests" 체크
- GitHub Variables: `DRAFT_SCHEDULE_ENABLED=true` (초안 PR 예약 실행을 켤 때)
- 공개 전: `config.yaml` 의 `site.domain`·`site.url`·`site.contact_email` 채우기

## 테스트

```bash
python tests/test_keywords.py && python tests/test_stage2.py && python tests/test_build.py && python tests/test_open_pr.py
```
