#!/usr/bin/env python3
"""Run deterministic checks for the dependency-free MiniMind guide examples."""

from __future__ import annotations

import json
import math
from pathlib import Path

import model_config_check
import sampling_lab
import validate_data


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> int:
    passed = 0

    defaults = model_config_check.declared_defaults(model_config_check.DEFAULT_SOURCE)
    check(
        not model_config_check.compare_source_defaults(defaults),
        "guide defaults drifted from MiniMindConfig source",
    )
    passed += 1

    dense = model_config_check.estimate_parameters(model_config_check.ModelPlan())
    check(dense["total"] == 63_912_192, f"unexpected dense count: {dense['total']}")
    passed += 1

    moe = model_config_check.estimate_parameters(
        model_config_check.ModelPlan(use_moe=True)
    )
    check(moe["total"] == 198_416_640, f"unexpected MoE count: {moe['total']}")
    passed += 1

    non_canonical = model_config_check.ModelPlan(hidden_size=770)
    check(
        not non_canonical.validate() and non_canonical.canonical_warnings(),
        "non-divisible hidden width must be valid with a canonical warning",
    )
    passed += 1

    invalid_expert = model_config_check.ModelPlan(
        num_experts=2, num_experts_per_tok=3
    ).validate()
    check(invalid_expert, "invalid expert top-k was not rejected")
    passed += 1

    invalid_kv = model_config_check.ModelPlan(num_key_value_heads=3).validate()
    check(invalid_kv, "incompatible query/KV head ratio was not rejected")
    passed += 1

    odd_rope = model_config_check.ModelPlan(head_dim=95).validate()
    check(odd_rope, "odd rotary head dimension was not rejected")
    passed += 1

    for kind in validate_data.KINDS:
        issues = validate_data.validate_records(kind, validate_data.DEMO_RECORDS[kind])
        check(not issues, f"{kind} demo failed validation: {issues}")
    passed += 1

    data_dir = Path(__file__).resolve().parent / "data"
    fixtures = {
        "pretrain": data_dir / "pretrain_tiny.jsonl",
        "sft": data_dir / "sft_tiny.jsonl",
    }
    for kind, path in fixtures.items():
        with path.open("r", encoding="utf-8") as handle:
            records = [json.loads(line) for line in handle if line.strip()]
        issues = validate_data.validate_records(kind, records)
        check(not issues, f"{kind} fixture failed validation: {issues}")
    passed += 1

    first = sampling_lab.run_experiment(seed=123)
    second = sampling_lab.run_experiment(seed=123)
    check(
        first["selected"] == second["selected"], "seeded sampling is not reproducible"
    )
    passed += 1

    check(
        math.isclose(float(first["probability_sum"]), 1.0, rel_tol=0.0, abs_tol=1e-12),
        "sampling probabilities do not sum to one",
    )
    check(0 < len(first["candidates"]) <= 4, "top-k/top-p candidate bound failed")
    passed += 1

    print(f"MiniMind guide smoke test: {passed} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
