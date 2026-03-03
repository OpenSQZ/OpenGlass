from __future__ import annotations

import pandas as pd
from typing import Optional


def load_manifest(path: str, n_samples: Optional[int] = None, seed: int = 0) -> pd.DataFrame:
    df = pd.read_csv(path)
    if n_samples is not None:
        df = df.sample(n=min(n_samples, len(df)), random_state=seed).reset_index(drop=True)
    return df
