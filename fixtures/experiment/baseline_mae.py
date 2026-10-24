"""Seasonal-naive MAE on a weekly demand CSV. Stdlib only and deterministic for a given seed."""

import argparse
import csv
import hashlib
import json
import random
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default="data.csv")
    parser.add_argument("--out", default="results.json")
    parser.add_argument("--season", type=int, default=4)
    parser.add_argument("--seed", type=int, required=True)
    args = parser.parse_args()

    raw = Path(args.data).read_bytes()
    demand = [float(row["demand"]) for row in csv.DictReader(raw.decode("utf-8").splitlines())]
    errors = [abs(demand[t] - demand[t - args.season]) for t in range(args.season, len(demand))]
    mae = sum(errors) / len(errors)

    rng = random.Random(args.seed)
    boots = sorted(
        sum(rng.choices(errors, k=len(errors))) / len(errors) for _ in range(200)
    )
    results = {
        "method": "seasonal_naive",
        "metric": "mae",
        "season": args.season,
        "seed": args.seed,
        "n_forecasts": len(errors),
        "mae": round(mae, 6),
        "mae_ci95": [round(boots[4], 6), round(boots[194], 6)],
        "data_sha256": hashlib.sha256(raw).hexdigest(),
    }
    Path(args.out).write_text(json.dumps(results, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"seasonal-naive MAE = {results['mae']} over {len(errors)} forecasts (seed {args.seed})")


if __name__ == "__main__":
    main()
