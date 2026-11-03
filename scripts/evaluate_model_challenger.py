"""Produce an immutable, non-promoting same-split challenger decision."""

from __future__ import annotations

import argparse
from pathlib import Path

from texas_energy.mlops.model_lifecycle import evaluate_challenger, load_json, write_new_json


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--champion", type=Path, required=True)
    parser.add_argument("--challenger", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    decision = evaluate_challenger(champion=load_json(args.champion), challenger=load_json(args.challenger))
    write_new_json(args.output, decision)
    print(f"{args.output} {decision['decision']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
