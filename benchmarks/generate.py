"""Synthetic benchmark datasets with fixed seeds.

    python -m benchmarks.generate medium out.xlsx
"""

from __future__ import annotations

import argparse
import random
from dataclasses import dataclass
from pathlib import Path

import openpyxl

from rebar_optimizer.domain.models import Piece

SIZE_NAMES = ("DB12", "DB16", "DB20", "DB25", "RB9", "DB10", "DB28", "RB6")


@dataclass(frozen=True)
class Preset:
    sizes: int
    lengths_per_size: int
    total_pieces: int
    min_length_mm: int = 300
    max_length_mm: int = 7500
    step_mm: int = 50


PRESETS: dict[str, Preset] = {
    "small": Preset(sizes=2, lengths_per_size=6, total_pieces=60),
    "medium": Preset(sizes=5, lengths_per_size=15, total_pieces=500),
    "large": Preset(sizes=5, lengths_per_size=40, total_pieces=2000),
}


def make_pieces(preset: Preset, seed: int = 0) -> list[Piece]:
    """Deterministic pieces for ``preset``; piece numbers are unique across sizes."""
    rng = random.Random(seed)
    per_size = preset.total_pieces // preset.sizes
    pieces: list[Piece] = []
    number = 0
    for size in SIZE_NAMES[: preset.sizes]:
        lengths = rng.sample(
            range(preset.min_length_mm, preset.max_length_mm + 1, preset.step_mm),
            preset.lengths_per_size,
        )
        # Split per_size pieces over the lengths, at least one each.
        cuts = sorted(rng.sample(range(1, per_size), preset.lengths_per_size - 1))
        quantities = [b - a for a, b in zip([0, *cuts], [*cuts, per_size], strict=True)]
        for length, quantity in zip(lengths, quantities, strict=True):
            number += 1
            pieces.append(Piece(size, str(number), length, quantity, (number + 1,)))
    return pieces


def write_xlsx(pieces: list[Piece], path: Path) -> None:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(("Size", "No", "Length", "Quantity"))
    for p in pieces:
        sheet.append((p.size, p.no, p.length_mm / 1000, p.quantity))
    workbook.save(path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("preset", choices=sorted(PRESETS))
    parser.add_argument("output", type=Path)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()
    write_xlsx(make_pieces(PRESETS[args.preset], args.seed), args.output)


if __name__ == "__main__":
    main()
