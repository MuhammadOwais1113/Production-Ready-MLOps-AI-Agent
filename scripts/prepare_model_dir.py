"""Copy the trained model + scaler into ./models so the Dockerfile can bake them into the image."""
from __future__ import annotations

import shutil
from pathlib import Path

root = Path(__file__).resolve().parents[1]
dst = root / "models"
dst.mkdir(exist_ok=True)
for f in ("model.joblib", "scaler.joblib"):
    src = root / "artifacts" / f
    if not src.exists():
        raise SystemExit(f"{src} missing - run `python -m training.train` first")
    shutil.copy(src, dst / f)
print("models/ ready")
