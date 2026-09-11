#!/usr/bin/env python3
"""Small, dependency-free demonstration of MiniMind-style token filtering."""

from __future__ import annotations

import argparse
import json
import math
import random
from collections.abc import Sequence

DEFAULT_TOKENS = ("graph", "model", "data", "agent", "token", "cache")
DEFAULT_LOGITS = (4.0, 3.1, 2.4, 1.9, 0.5, -0.2)


def softmax(logits: Sequence[float]) -> list[float]:
    finite = [value for value in logits if math.isfinite(value)]
    if not finite:
        raise ValueError("at least one finite logit is required")
    maximum = max(finite)
    weights = [
        math.exp(value - maximum) if math.isfinite(value) else 0.0 for value in logits
    ]
    total = sum(weights)
    return [weight / total for weight in weights]


def apply_repetition_penalty(
    logits: Sequence[float], seen_indices: set[int], penalty: float
) -> list[float]:
    if penalty <= 0:
        raise ValueError("repetition penalty must be positive")
    adjusted = list(logits)
    if penalty == 1.0:
        return adjusted
    for index in seen_indices:
        if not 0 <= index < len(adjusted):
            raise ValueError(f"seen index out of range: {index}")
        score = adjusted[index]
        adjusted[index] = score / penalty if score > 0 else score * penalty
    return adjusted


def filter_top_k(logits: Sequence[float], top_k: int) -> list[float]:
    if top_k < 0:
        raise ValueError("top_k must be non-negative")
    if top_k == 0 or top_k >= len(logits):
        return list(logits)
    keep = set(
        sorted(range(len(logits)), key=lambda i: logits[i], reverse=True)[:top_k]
    )
    return [value if index in keep else -math.inf for index, value in enumerate(logits)]


def filter_top_p(logits: Sequence[float], top_p: float) -> list[float]:
    if not 0 < top_p <= 1:
        raise ValueError("top_p must be in (0, 1]")
    if top_p == 1:
        return list(logits)
    probabilities = softmax(logits)
    ranked = sorted(range(len(logits)), key=lambda i: logits[i], reverse=True)
    keep: set[int] = set()
    cumulative = 0.0
    for index in ranked:
        if not math.isfinite(logits[index]):
            continue
        keep.add(index)
        cumulative += probabilities[index]
        # MiniMind shifts the cumsum mask, so the first token crossing p remains.
        if cumulative >= top_p:
            break
    return [value if index in keep else -math.inf for index, value in enumerate(logits)]


def weighted_choice(probabilities: Sequence[float], rng: random.Random) -> int:
    threshold = rng.random()
    cumulative = 0.0
    for index, probability in enumerate(probabilities):
        cumulative += probability
        if threshold <= cumulative:
            return index
    return len(probabilities) - 1


def run_experiment(
    *,
    tokens: Sequence[str] = DEFAULT_TOKENS,
    logits: Sequence[float] = DEFAULT_LOGITS,
    seen_tokens: Sequence[str] = ("graph", "data"),
    temperature: float = 0.8,
    repetition_penalty: float = 1.2,
    top_k: int = 4,
    top_p: float = 0.8,
    seed: int = 42,
    greedy: bool = False,
) -> dict[str, object]:
    if len(tokens) != len(logits) or not tokens:
        raise ValueError("tokens and logits must be non-empty and have equal length")
    if len(set(tokens)) != len(tokens):
        raise ValueError("tokens must be unique in this toy example")
    if temperature <= 0:
        raise ValueError("temperature must be positive")

    token_to_index = {token: index for index, token in enumerate(tokens)}
    unknown = set(seen_tokens) - token_to_index.keys()
    if unknown:
        raise ValueError(f"unknown seen tokens: {sorted(unknown)}")

    tempered = [value / temperature for value in logits]
    repeated = apply_repetition_penalty(
        tempered, {token_to_index[token] for token in seen_tokens}, repetition_penalty
    )
    top_k_logits = filter_top_k(repeated, top_k)
    final_logits = filter_top_p(top_k_logits, top_p)
    probabilities = softmax(final_logits)
    selected_index = (
        max(range(len(final_logits)), key=lambda i: final_logits[i])
        if greedy
        else weighted_choice(probabilities, random.Random(seed))
    )

    candidates = [
        {
            "token": token,
            "original_logit": logits[index],
            "filtered_logit": round(final_logits[index], 6),
            "probability": round(probabilities[index], 8),
        }
        for index, token in enumerate(tokens)
        if math.isfinite(final_logits[index])
    ]
    return {
        "settings": {
            "temperature": temperature,
            "repetition_penalty": repetition_penalty,
            "top_k": top_k,
            "top_p": top_p,
            "seed": seed,
            "mode": "greedy" if greedy else "multinomial",
            "seen_tokens": list(seen_tokens),
        },
        "candidates": candidates,
        "selected": tokens[selected_index],
        "probability_sum": sum(probabilities),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a MiniMind-style toy sampling step."
    )
    parser.add_argument("--temperature", type=float, default=0.8)
    parser.add_argument("--repetition-penalty", type=float, default=1.2)
    parser.add_argument("--top-k", type=int, default=4)
    parser.add_argument("--top-p", type=float, default=0.8)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--seen", nargs="*", default=["graph", "data"])
    parser.add_argument("--greedy", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    result = run_experiment(
        seen_tokens=args.seen,
        temperature=args.temperature,
        repetition_penalty=args.repetition_penalty,
        top_k=args.top_k,
        top_p=args.top_p,
        seed=args.seed,
        greedy=args.greedy,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
