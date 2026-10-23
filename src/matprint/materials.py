"""Material database: loading, lookup and the conservative-value rules."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Optional

import yaml

CHEM_CATEGORIES = ("acids", "bases", "water", "solvents")
CHEM_LEVELS = {"not": 0, "short": 1, "resistant": 2}


@dataclass
class Shrinkage:
    """Linear XY shrinkage in percent."""

    typical: Optional[float]
    min: Optional[float]
    max: Optional[float]
    source: str = ""

    @property
    def known(self) -> bool:
        return None not in (self.typical, self.min, self.max)


@dataclass
class Material:
    name: str
    base: str
    filler: Optional[str]
    shrinkage: Shrinkage
    hole_offset_mm: Optional[float]
    contour_offset_mm: float
    max_use_temp_c: list[dict]
    chemical: dict[str, str]
    uv_resistant: Optional[bool]
    tensile_mpa: Optional[float]
    modulus_gpa: Optional[float]
    elongation_pct: Optional[float]
    resistivity_ohm_cm: Optional[list[float]]
    min_feature_mm: Optional[dict]
    min_wall_mm: Optional[float]
    infill_pct: Optional[dict]
    notes: str = ""
    sources: list[str] = field(default_factory=list)

    @property
    def fibre_filled(self) -> bool:
        return bool(self.filler) and "fibre" in self.filler.lower()

    @property
    def conductive(self) -> bool:
        return self.resistivity_ohm_cm is not None

    @property
    def max_temp_conservative(self) -> Optional[float]:
        """Lowest max use temperature across sources, or None if not given."""
        values = [t["value"] for t in self.max_use_temp_c if t.get("value") is not None]
        return min(values) if values else None

    def chem_level(self, category: str) -> Optional[int]:
        """0 = not resistant, 1 = short exposure, 2 = resistant, None = unknown."""
        return CHEM_LEVELS.get(self.chemical.get(category, "unknown"))

    def infill_range(self, purpose: str = "functional") -> Optional[tuple[float, float]]:
        if not self.infill_pct:
            return None
        rng = self.infill_pct.get(purpose) or self.infill_pct.get("functional")
        return (float(rng[0]), float(rng[1])) if rng else None


def _norm(name: str) -> str:
    n = re.sub(r"[\s\-_]+", "", name.lower())
    return n.replace("nylon", "")


class MaterialDB:
    def __init__(self, materials: list[Material]):
        self.materials = materials
        self._index = {_norm(m.name): m for m in materials}

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "MaterialDB":
        if path is None:
            text = resources.files("matprint.data").joinpath("materials.yaml").read_text("utf-8")
        else:
            text = Path(path).read_text("utf-8")
        raw = yaml.safe_load(text)
        mats = []
        for r in raw["materials"]:
            r = dict(r)
            r["shrinkage"] = Shrinkage(**r["shrinkage"])
            r.setdefault("contour_offset_mm", 0.0)
            r["contour_offset_mm"] = r["contour_offset_mm"] or 0.0
            r["max_use_temp_c"] = r.get("max_use_temp_c") or []
            chem = {c: "unknown" for c in CHEM_CATEGORIES}
            chem.update(r.get("chemical") or {})
            r["chemical"] = chem
            mats.append(Material(**r))
        return cls(mats)

    def get(self, name: str) -> Material:
        key = _norm(name)
        if key in self._index:
            return self._index[key]
        matches = [m for k, m in self._index.items() if key and key in k]
        if len(matches) == 1:
            return matches[0]
        known = ", ".join(m.name for m in self.materials)
        raise KeyError(f"Unknown material '{name}'. Known materials: {known}")

    def __iter__(self):
        return iter(self.materials)
