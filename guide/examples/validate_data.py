#!/usr/bin/env python3
"""Validate MiniMind JSONL shapes without loading datasets, tokenizers, or tools."""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

KINDS = ("pretrain", "sft", "dpo", "rlaif", "agent")
ALLOWED_ROLES = {"system", "user", "assistant", "tool"}


DEMO_RECORDS: dict[str, list[dict[str, Any]]] = {
    "pretrain": [{"text": "MiniMind는 다음 토큰을 예측하는 작은 언어 모델이다."}],
    "sft": [
        {
            "conversations": [
                {"role": "user", "content": "RMSNorm을 한 문장으로 설명해줘."},
                {
                    "role": "assistant",
                    "content": "벡터의 RMS 크기로 정규화하는 층입니다.",
                },
            ]
        }
    ],
    "dpo": [
        {
            "chosen": [
                {"role": "user", "content": "2 더하기 2는?"},
                {"role": "assistant", "content": "4입니다."},
            ],
            "rejected": [
                {"role": "user", "content": "2 더하기 2는?"},
                {"role": "assistant", "content": "5입니다."},
            ],
        }
    ],
    "rlaif": [
        {
            "conversations": [
                {"role": "user", "content": "안전한 비밀번호 원칙 세 가지를 말해줘."},
                {
                    "role": "assistant",
                    "content": "길이, 고유성, 비밀 관리가 중요합니다.",
                },
            ]
        }
    ],
    "agent": [
        {
            "conversations": [
                {
                    "role": "system",
                    "content": "필요하면 허용된 도구를 사용하라.",
                    "tools": json.dumps(
                        [
                            {
                                "type": "function",
                                "function": {
                                    "name": "calculate_math",
                                    "parameters": {"type": "object"},
                                },
                            }
                        ],
                        ensure_ascii=False,
                    ),
                },
                {"role": "user", "content": "7의 제곱은?"},
                {"role": "assistant", "content": "49"},
            ],
            "gt": ["49"],
        }
    ],
}


def decode_json_field(value: Any, field: str, errors: list[str]) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            errors.append(f"{field} is not valid JSON text: {exc.msg}")
            return None
    return value


def validate_messages(
    value: Any,
    field: str,
    *,
    require_assistant: bool = True,
    require_final_assistant: bool = False,
) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, list) or not value:
        return [f"{field} must be a non-empty list"]

    assistant_count = 0
    for index, message in enumerate(value):
        prefix = f"{field}[{index}]"
        if not isinstance(message, dict):
            errors.append(f"{prefix} must be an object")
            continue
        role = message.get("role")
        if role not in ALLOWED_ROLES:
            errors.append(f"{prefix}.role must be one of {sorted(ALLOWED_ROLES)}")
        if role == "assistant":
            assistant_count += 1
        if "content" not in message or not isinstance(message.get("content"), str):
            errors.append(f"{prefix}.content must be a string (empty is allowed)")
        reasoning = message.get("reasoning_content")
        if reasoning is not None and not isinstance(reasoning, str):
            errors.append(f"{prefix}.reasoning_content must be a string when present")

        for optional_field in ("tools", "tool_calls"):
            if optional_field not in message or message[optional_field] in (None, ""):
                continue
            decoded = decode_json_field(
                message[optional_field], f"{prefix}.{optional_field}", errors
            )
            if decoded is not None and not isinstance(decoded, (list, dict)):
                errors.append(
                    f"{prefix}.{optional_field} JSON must decode to list/object"
                )

    if require_assistant and assistant_count == 0:
        errors.append(f"{field} must contain at least one assistant message")
    if require_final_assistant and (
        not isinstance(value[-1], dict) or value[-1].get("role") != "assistant"
    ):
        errors.append(
            f"{field} must end with assistant because the dataset removes the final answer"
        )
    return errors


def preference_context(messages: list[Any]) -> list[Any]:
    if (
        messages
        and isinstance(messages[-1], dict)
        and messages[-1].get("role") == "assistant"
    ):
        return messages[:-1]
    return messages


def validate_record(kind: str, record: Any) -> list[str]:
    if not isinstance(record, dict):
        return ["record must be a JSON object"]

    if kind == "pretrain":
        text = record.get("text")
        return (
            []
            if isinstance(text, str) and text.strip()
            else ["text must be a non-empty string"]
        )

    if kind in {"sft", "rlaif", "agent"}:
        errors = validate_messages(
            record.get("conversations"),
            "conversations",
            require_final_assistant=kind in {"rlaif", "agent"},
        )
        if kind == "agent":
            gt = record.get("gt")
            if not isinstance(gt, list) or not gt:
                errors.append("gt must be a non-empty list")
            elif any(not isinstance(item, (str, int, float)) for item in gt):
                errors.append("gt items must be strings or numbers")
        return errors

    if kind == "dpo":
        chosen = record.get("chosen")
        rejected = record.get("rejected")
        errors = validate_messages(chosen, "chosen", require_final_assistant=True)
        errors.extend(
            validate_messages(rejected, "rejected", require_final_assistant=True)
        )
        if (
            isinstance(chosen, list)
            and isinstance(rejected, list)
            and preference_context(chosen) != preference_context(rejected)
        ):
            errors.append(
                "chosen and rejected must share the same context before the final answer"
            )
        return errors

    return [f"unsupported kind: {kind}"]


def validate_records(
    kind: str, records: Iterable[Any], line_numbers: Iterable[int] | None = None
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    materialized = list(records)
    numbers = (
        list(line_numbers)
        if line_numbers is not None
        else list(range(1, len(materialized) + 1))
    )
    if len(numbers) != len(materialized):
        raise ValueError("line_numbers and records must have equal length")
    for line_number, record in zip(numbers, materialized):
        for message in validate_record(kind, record):
            issues.append({"line": line_number, "message": message})
    return issues


def read_jsonl(
    path: Path, max_errors: int
) -> tuple[list[Any], list[int], list[dict[str, Any]]]:
    records: list[Any] = []
    line_numbers: list[int] = []
    issues: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            if not raw_line.strip():
                issues.append({"line": line_number, "message": "blank line"})
                if len(issues) >= max_errors:
                    break
                continue
            try:
                records.append(json.loads(raw_line))
                line_numbers.append(line_number)
            except json.JSONDecodeError as exc:
                issues.append(
                    {"line": line_number, "message": f"invalid JSON: {exc.msg}"}
                )
                if len(issues) >= max_errors:
                    break
    return records, line_numbers, issues


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate MiniMind training JSONL without executing a tokenizer or tools."
    )
    parser.add_argument("path", type=Path, nargs="?")
    parser.add_argument("--kind", choices=(*KINDS, "all"), required=True)
    parser.add_argument("--max-errors", type=int, default=20)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.max_errors <= 0:
        raise SystemExit("--max-errors must be positive")
    if args.path is not None and args.kind == "all":
        raise SystemExit("--kind all is only available for the built-in demos")

    kinds = KINDS if args.kind == "all" else (args.kind,)
    reports: list[dict[str, Any]] = []
    failed = False
    for kind in kinds:
        if args.path is None:
            records = DEMO_RECORDS[kind]
            line_numbers = None
            parse_issues: list[dict[str, Any]] = []
            source = "built-in demo"
        else:
            records, line_numbers, parse_issues = read_jsonl(args.path, args.max_errors)
            source = str(args.path.resolve())
        issues = parse_issues + validate_records(kind, records, line_numbers)
        issues = issues[: args.max_errors]
        failed |= bool(issues)
        reports.append(
            {
                "kind": kind,
                "source": source,
                "records_checked": len(records),
                "valid": not issues,
                "issues": issues,
            }
        )

    print(json.dumps(reports, ensure_ascii=False, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
