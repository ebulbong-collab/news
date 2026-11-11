"""Claude API 호출 공통 래퍼: 구조화 출력(JSON Schema) + 지시서 6-3의 재시도 규칙.

- 429·5xx·529·연결 오류는 api.retry_wait_sec 간격으로 api.retry_count 회 재시도한 뒤 예외를 올린다(그날은 중단).
- 거절(refusal)·max_tokens 로 끊긴 응답은 예외로 올린다. 불완전한 JSON 을 조용히 쓰지 않는다.
- API 키는 ANTHROPIC_API_KEY 환경변수에서만 읽는다(SDK 기본 동작). 코드와 로그에 남기지 않는다.
"""
from __future__ import annotations

import json
import sys
import time

import anthropic

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMError(RuntimeError):
    """응답이 완결되지 않았거나 거절된 경우."""


def call_structured(cfg: dict, *, model: str, system: str, user: str, schema: dict, effort: str,
                    max_tokens: int, fallbacks: bool = False) -> tuple[dict, dict]:
    """(파싱된 JSON, usage). fallbacks=True 면 서버측 거절 폴백(beta)을 켠다(Sonnet 5.5 용)."""
    api = cfg["api"]
    client = anthropic.Anthropic(max_retries=0)  # 재시도는 아래에서 직접 한다
    kwargs = dict(
        model=model, max_tokens=max_tokens, system=system,
        messages=[{"role": "user", "content": user}],
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
    )

    resp = None
    for attempt in range(api["retry_count"] + 1):
        try:
            if fallbacks:
                resp = client.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **kwargs)
            else:
                resp = client.messages.create(**kwargs)
            break
        except (anthropic.RateLimitError, anthropic.InternalServerError,
                anthropic.APIConnectionError) as e:
            if attempt >= api["retry_count"]:
                raise
            print(f"[재시도 {attempt + 1}/{api['retry_count']}] {type(e).__name__} — "
                  f"{api['retry_wait_sec']}초 후", file=sys.stderr)
            time.sleep(api["retry_wait_sec"])
        except anthropic.APIStatusError as e:
            if e.status_code == 529 and attempt < api["retry_count"]:  # overloaded
                time.sleep(api["retry_wait_sec"])
                continue
            raise

    if resp.stop_reason == "refusal":
        cat = getattr(getattr(resp, "stop_details", None), "category", None)
        raise LLMError(f"모델이 응답을 거절했다 (category={cat})")
    if resp.stop_reason == "max_tokens":
        raise LLMError("max_tokens 에서 응답이 끊겼다 — 출력 상한을 늘리거나 입력을 줄여야 한다")
    text = next((b.text for b in resp.content if b.type == "text"), None)
    if text is None:
        raise LLMError(f"텍스트 블록이 없다: stop_reason={resp.stop_reason}")
    usage = {
        "model": model, "served_model": getattr(resp, "model", model),
        "request_id": getattr(resp, "_request_id", None),
        "input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens,
    }
    return json.loads(text), usage
