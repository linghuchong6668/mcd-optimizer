"""麦当劳餐品营养数据的解析与匹配。

list-nutrition-foods 返回的是一段紧凑文本，形如：
    [160]{productName,nutritionDescription,energyKj,energyKcal,protein,fat,carbohydrate,sodium,calcium}:
      猪柳麦满分,null,1288,308,16,16,24,781,213

这里把它解析成结构化字典，并提供「菜单餐品名 → 营养条目」的模糊匹配。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

FIELDS = [
    "productName",
    "nutritionDescription",
    "energyKj",
    "energyKcal",
    "protein",
    "fat",
    "carbohydrate",
    "sodium",
    "calcium",
]


@dataclass(frozen=True)
class Nutrition:
    name: str
    kcal: float
    protein: float
    fat: float
    carb: float
    sodium: float
    calcium: float


def _to_float(value: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def parse_nutrition_text(text: str) -> list[Nutrition]:
    """从 list-nutrition-foods 的返回文本中解析出营养条目。"""
    raw = text
    if isinstance(text, dict):
        raw = text.get("_text", "") or text.get("data", "")
    rows: list[Nutrition] = []
    for line in str(raw).splitlines():
        line = line.strip()
        if not line or line.startswith("[") or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < len(FIELDS):
            continue
        name = parts[0]
        if not name or name == "productName":
            continue
        rows.append(
            Nutrition(
                name=name,
                kcal=_to_float(parts[3]),
                protein=_to_float(parts[4]),
                fat=_to_float(parts[5]),
                carb=_to_float(parts[6]),
                sodium=_to_float(parts[7]),
                calcium=_to_float(parts[8]),
            )
        )
    return rows


_NOISE = re.compile(r"[\s（）()·、/\\]+")


def normalize(name: str) -> str:
    return _NOISE.sub("", str(name)).lower()


class NutritionIndex:
    """把营养表建成索引，支持菜单餐品名的最长子串匹配。"""

    def __init__(self, rows: list[Nutrition]):
        self.rows = rows
        # 长名字优先，避免「薯条」抢先匹配掉「大薯条」
        self._sorted = sorted(rows, key=lambda r: len(r.name), reverse=True)

    def match(self, meal_name: str) -> Nutrition | None:
        target = normalize(meal_name)
        for row in self._sorted:
            if normalize(row.name) in target:
                return row
        return None

    def match_many(self, meal_name: str) -> list[Nutrition]:
        """找出餐品名中包含的所有营养条目，用于「三件套」这类组合餐的营养累加。"""
        target = normalize(meal_name)
        hits = [row for row in self._sorted if normalize(row.name) in target]
        # 去掉被更长条目包含的重复项（例如「薯条」被「大薯条」包含）
        kept: list[Nutrition] = []
        for hit in hits:
            if any(hit is not other and normalize(hit.name) in normalize(other.name) for other in hits):
                continue
            kept.append(hit)
        return kept
