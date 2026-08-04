"""Oracle parity: LMS engine vs published UK-WHO values, +/-0.01 z (task R4).

Permanent regression collateral - never delete once populated."""

import csv
from pathlib import Path

VECTORS = Path(__file__).parent / "ukwho_vectors.csv"


def test_ukwho_parity() -> None:
    with VECTORS.open() as f:
        rows = [r for r in csv.DictReader(f) if not r["measure"].startswith("#")]
    if not rows:
        return  # unpopulated until R2/R4; becomes a hard gate once vectors land
    raise NotImplementedError("task R4")
