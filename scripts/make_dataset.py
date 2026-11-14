"""Generate a reproducible synthetic classification dataset (the DVC-tracked data).

  python scripts/make_dataset.py --version 1            # 1000 rows
  python scripts/make_dataset.py --version 2            # 1500 rows, different distribution
  python scripts/make_dataset.py --version 1 --corrupt  # injected failure: NaNs in feature columns
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.datasets import make_classification

CONFIG = {1: dict(n_samples=1000, random_state=1, class_sep=1.2), 2: dict(n_samples=1500, random_state=2, class_sep=1.0)}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--version", type=int, default=1, choices=sorted(CONFIG))
    p.add_argument("--out", default="data/dataset.csv")
    p.add_argument("--corrupt", action="store_true")
    a = p.parse_args()
    x, y = make_classification(n_features=8, n_informative=5, n_redundant=1, flip_y=0.03, **CONFIG[a.version])
    df = pd.DataFrame(x, columns=[f"f{i}" for i in range(8)]).round(5)
    df["label"] = y
    if a.corrupt:
        rng = np.random.default_rng(0)
        df.loc[rng.choice(len(df), size=len(df) // 4, replace=False), "f3"] = np.nan
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(a.out, index=False)
    print(f"wrote {a.out}: {len(df)} rows (version {a.version}{', CORRUPTED' if a.corrupt else ''})")


if __name__ == "__main__":
    main()
