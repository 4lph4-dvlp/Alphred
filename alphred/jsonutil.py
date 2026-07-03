"""JSON 추출 유틸 — LLM 응답에서 JSON 오브젝트를 견고하게 파싱한다.

LLM 응답은 ```json 펜스나 앞뒤 산문을 포함할 수 있으므로, 모든 '{' 위치에서
raw_decode 를 시도해 가장 먼저 성공하는 오브젝트를 취한다. greedy 정규식
(r"\\{.*\\}") 과 달리 본문에 중괄호가 섞이거나 뒤에 잡문이 붙어도 깨지지 않는다.
"""
from __future__ import annotations

import json

_DECODER = json.JSONDecoder()


def parse_json_object(text: str) -> dict | None:
    """텍스트에서 첫 번째 최상위 JSON 오브젝트를 파싱해 dict 로 반환. 실패 시 None."""
    if not text:
        return None
    idx = 0
    while True:
        start = text.find("{", idx)
        if start == -1:
            return None
        try:
            obj, _ = _DECODER.raw_decode(text, start)
        except ValueError:
            idx = start + 1
            continue
        if isinstance(obj, dict):
            return obj
        idx = start + 1
