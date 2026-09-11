#!/usr/bin/env python3
"""Inspect MiniMind config defaults and estimate parameter counts without PyTorch.

This is an educational preflight check, not a checkpoint compatibility guarantee.
It uses only the Python standard library and reads the repository source via AST.
"""

from __future__ import annotations

import argparse
import ast
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE = REPOSITORY_ROOT / "model" / "model_minimind.py"


@dataclass(frozen=True)
class ModelPlan:
    hidden_size: int = 768
    num_hidden_layers: int = 8
    vocab_size: int = 6400
    num_attention_heads: int = 8
    num_key_value_heads: int = 4
    head_dim: int = 0
    intermediate_size: int = 0
    max_position_embeddings: int = 32768
    use_moe: bool = False
    num_experts: int = 4
    num_experts_per_tok: int = 1
    moe_intermediate_size: int = 0
    tie_word_embeddings: bool = True

    def resolved(self) -> ModelPlan:
        head_dim = self.head_dim or self.hidden_size // self.num_attention_heads
        intermediate_size = self.intermediate_size or (
            math.ceil(self.hidden_size * math.pi / 64) * 64
        )
        moe_intermediate_size = self.moe_intermediate_size or intermediate_size
        return ModelPlan(
            **{
                **asdict(self),
                "head_dim": head_dim,
                "intermediate_size": intermediate_size,
                "moe_intermediate_size": moe_intermediate_size,
            }
        )

    def validate(self) -> list[str]:
        errors: list[str] = []
        positive = {
            "hidden_size": self.hidden_size,
            "num_hidden_layers": self.num_hidden_layers,
            "vocab_size": self.vocab_size,
            "num_attention_heads": self.num_attention_heads,
            "num_key_value_heads": self.num_key_value_heads,
            "max_position_embeddings": self.max_position_embeddings,
            "num_experts": self.num_experts,
            "num_experts_per_tok": self.num_experts_per_tok,
        }
        for name, value in positive.items():
            if value <= 0:
                errors.append(f"{name} must be positive, got {value}")

        if self.num_key_value_heads > 0 and (
            self.num_attention_heads % self.num_key_value_heads != 0
        ):
            errors.append(
                "num_attention_heads must be divisible by num_key_value_heads "
                "for repeat_kv"
            )

        resolved_head_dim = self.head_dim
        if resolved_head_dim == 0 and self.num_attention_heads > 0:
            resolved_head_dim = self.hidden_size // self.num_attention_heads
        if resolved_head_dim <= 0:
            errors.append("resolved head_dim must be positive")
        elif resolved_head_dim % 2:
            errors.append("head_dim must be even for paired rotary dimensions")

        if self.num_experts_per_tok > self.num_experts:
            errors.append("num_experts_per_tok cannot exceed num_experts")
        if self.intermediate_size < 0 or self.moe_intermediate_size < 0:
            errors.append("intermediate sizes must be zero (auto) or positive")
        return errors

    def canonical_warnings(self) -> list[str]:
        """Return interoperability warnings that are not runtime shape errors."""

        warnings: list[str] = []
        if self.num_attention_heads <= 0:
            return warnings
        resolved_head_dim = (
            self.head_dim or self.hidden_size // self.num_attention_heads
        )
        projected_width = self.num_attention_heads * resolved_head_dim
        if projected_width != self.hidden_size:
            warnings.append(
                "query_heads * resolved head_dim differs from hidden_size "
                f"({projected_width} != {self.hidden_size}); MiniMind's projections "
                "can execute this non-canonical width, but ecosystem conversion and "
                "checkpoint assumptions may not"
            )
        return warnings


def estimate_parameters(plan: ModelPlan) -> dict[str, int]:
    """Estimate parameters represented by the current MiniMind module structure."""

    p = plan.resolved()
    h, d = p.hidden_size, p.head_dim

    embedding = p.vocab_size * h
    lm_head = 0 if p.tie_word_embeddings else p.vocab_size * h

    # q/k/v/o projections have no bias. Q-Norm and K-Norm each own head_dim weights.
    attention_per_layer = (
        h * (p.num_attention_heads * d)
        + 2 * h * (p.num_key_value_heads * d)
        + (p.num_attention_heads * d) * h
        + 2 * d
    )
    block_norms_per_layer = 2 * h

    if p.use_moe:
        ffn_per_layer = (
            h * p.num_experts + p.num_experts * 3 * h * p.moe_intermediate_size
        )
    else:
        ffn_per_layer = 3 * h * p.intermediate_size

    transformer = p.num_hidden_layers * (
        attention_per_layer + block_norms_per_layer + ffn_per_layer
    )
    final_norm = h
    total = embedding + lm_head + transformer + final_norm
    return {
        "embedding": embedding,
        "attention_all_layers": p.num_hidden_layers * attention_per_layer,
        "ffn_all_layers": p.num_hidden_layers * ffn_per_layer,
        "block_norms_all_layers": p.num_hidden_layers * block_norms_per_layer,
        "final_norm": final_norm,
        "lm_head_unshared": lm_head,
        "total": total,
    }


def declared_defaults(source_path: Path) -> dict[str, Any]:
    """Read literal/default expressions from MiniMindConfig.__init__ using AST."""

    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    init_node: ast.FunctionDef | None = None
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "MiniMindConfig":
            init_node = next(
                (
                    child
                    for child in node.body
                    if isinstance(child, ast.FunctionDef) and child.name == "__init__"
                ),
                None,
            )
            break
    if init_node is None:
        raise ValueError("MiniMindConfig.__init__ not found")

    result: dict[str, Any] = {}
    named_args = init_node.args.args[1:]  # skip self
    if init_node.args.defaults:
        offset = len(named_args) - len(init_node.args.defaults)
        for arg, value in zip(named_args[offset:], init_node.args.defaults):
            try:
                result[arg.arg] = ast.literal_eval(value)
            except (ValueError, TypeError):
                result[arg.arg] = ast.unparse(value)

    for node in ast.walk(init_node):
        if not isinstance(node, ast.Call) or len(node.args) < 2:
            continue
        function = node.func
        if not (
            isinstance(function, ast.Attribute)
            and function.attr == "get"
            and isinstance(function.value, ast.Name)
            and function.value.id == "kwargs"
        ):
            continue
        try:
            key = ast.literal_eval(node.args[0])
        except (ValueError, TypeError):
            continue
        try:
            value = ast.literal_eval(node.args[1])
        except (ValueError, TypeError):
            value = ast.unparse(node.args[1])
        result[str(key)] = value
    return result


EXPECTED_SOURCE_DEFAULTS: dict[str, Any] = {
    "hidden_size": 768,
    "num_hidden_layers": 8,
    "use_moe": False,
    "vocab_size": 6400,
    "num_attention_heads": 8,
    "num_key_value_heads": 4,
    "max_position_embeddings": 32768,
    "rope_theta": 1_000_000.0,
    "tie_word_embeddings": True,
    "num_experts": 4,
    "num_experts_per_tok": 1,
}


def compare_source_defaults(actual: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        key: {"expected": expected, "actual": actual.get(key)}
        for key, expected in EXPECTED_SOURCE_DEFAULTS.items()
        if actual.get(key) != expected
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate a MiniMind model plan and estimate parameter counts."
    )
    parser.add_argument("--hidden-size", type=int, default=768)
    parser.add_argument("--layers", type=int, default=8)
    parser.add_argument("--vocab-size", type=int, default=6400)
    parser.add_argument("--query-heads", type=int, default=8)
    parser.add_argument("--kv-heads", type=int, default=4)
    parser.add_argument(
        "--head-dim", type=int, default=0, help="0 derives hidden/query"
    )
    parser.add_argument(
        "--intermediate-size", type=int, default=0, help="0 uses MiniMind rule"
    )
    parser.add_argument("--max-positions", type=int, default=32768)
    parser.add_argument("--moe", action="store_true")
    parser.add_argument("--experts", type=int, default=4)
    parser.add_argument("--experts-per-token", type=int, default=1)
    parser.add_argument("--moe-intermediate-size", type=int, default=0)
    parser.add_argument("--untied-embeddings", action="store_true")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument(
        "--strict-canonical",
        action="store_true",
        help="treat interoperability warnings as validation failures",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    plan = ModelPlan(
        hidden_size=args.hidden_size,
        num_hidden_layers=args.layers,
        vocab_size=args.vocab_size,
        num_attention_heads=args.query_heads,
        num_key_value_heads=args.kv_heads,
        head_dim=args.head_dim,
        intermediate_size=args.intermediate_size,
        max_position_embeddings=args.max_positions,
        use_moe=args.moe,
        num_experts=args.experts,
        num_experts_per_tok=args.experts_per_token,
        moe_intermediate_size=args.moe_intermediate_size,
        tie_word_embeddings=not args.untied_embeddings,
    )
    errors = plan.validate()
    canonical_warnings = plan.canonical_warnings() if not errors else []
    actual_defaults = declared_defaults(args.source)
    source_mismatches = compare_source_defaults(actual_defaults)
    counts = estimate_parameters(plan) if not errors else {}
    output = {
        "source": str(args.source.resolve()),
        "source_defaults_match_guide": not source_mismatches,
        "source_default_mismatches": source_mismatches,
        "plan": asdict(plan.resolved()) if not errors else asdict(plan),
        "validation_errors": errors,
        "canonical_warnings": canonical_warnings,
        "parameters": counts,
        "parameters_millions": round(counts.get("total", 0) / 1_000_000, 6),
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    strict_failure = args.strict_canonical and bool(canonical_warnings)
    return 1 if errors or source_mismatches or strict_failure else 0


if __name__ == "__main__":
    raise SystemExit(main())
