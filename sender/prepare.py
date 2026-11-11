"""오프라인 JSON → Menlo 브라우저 일괄 입력용 텍스트 변환기.

사용 예:
  python sender/prepare.py input.json > prepared.json

JSON 파일의 최상위 배열 또는 단일 이벤트 객체를 지원합니다.
복잡한 실제 파일 래퍼/삭제 방식은 사용자가 샘플 제공 후 별도 구현합니다.
외부 네트워크 접근/POST/브라우저 자동조작은 하지 않습니다.
"""
import argparse
import json
from pathlib import Path

USED = ("subject", "startTime", "endTime", "chair", "location")


def prepare(source):
    if isinstance(source, dict) and isinstance(source.get("events"), list):
        source = source["events"]
    if isinstance(source, dict):
        source = [source]
    if not isinstance(source, list):
        raise ValueError("JSON top level must be array, event object, or {events:[...]}")
    result = []
    for i, event in enumerate(source):
        if not isinstance(event, dict):
            raise ValueError(f"events[{i}] must be object")
        missing = [k for k in ("subject", "startTime", "endTime") if k not in event]
        if missing:
            raise ValueError(f"events[{i}] missing: {', '.join(missing)}")
        result.append({key: event[key] for key in USED if key in event})
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path, help="Local JSON file")
    args = parser.parse_args()
    raw = json.loads(args.path.read_text(encoding="utf-8"))
    print(json.dumps(prepare(raw), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
