"""CLI: run the full training pipeline locally (same stage functions the Airflow DAG calls).

  python -m training.train                       # normal run
  python -m training.train --degrade             # inject failure: weak model -> quality gate fails
  python -m training.train --inject-validation   # inject failure: validation stage fails
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

from training.pipeline import DataValidationError, QualityGateError, run_all


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--degrade", action="store_true")
    p.add_argument("--inject-validation", action="store_true")
    p.add_argument("--n-estimators", type=int)
    p.add_argument("--max-depth", type=int)
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    params: dict = {}
    if a.degrade:
        params["degrade_model"] = True
    if a.inject_validation:
        params["inject_failure"] = "validation"
    if a.n_estimators:
        params["n_estimators"] = a.n_estimators
    if a.max_depth:
        params["max_depth"] = a.max_depth
    try:
        print(json.dumps(run_all(params), indent=2, default=str))
        return 0
    except (DataValidationError, QualityGateError) as exc:
        print(f"PIPELINE BLOCKED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
