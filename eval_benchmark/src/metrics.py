from __future__ import annotations

import numpy as np
from typing import Dict, Iterable


def percentiles(x: Iterable[float], ps=(50, 90, 95)) -> Dict[str, float]:
    arr = np.asarray(list(x), dtype=float)
    arr = arr[np.isfinite(arr)]
    if arr.size == 0:
        return {f"p{p}": float("nan") for p in ps}
    out = {}
    for p in ps:
        out[f"p{p}"] = float(np.percentile(arr, p))
    return out
