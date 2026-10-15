#!/usr/bin/env python3
"""麦麦最优解 —— 在「预算 × 热量 × 人数 × 忌口」多重约束下，求解麦当劳最优下单方案。

它不是在菜单里随便挑几样，而是：
1. 用 MCP 拉取真实门店、真实菜单、真实价格、真实营养数据；
2. 在本地做约束建模与组合枚举（避免把 API 当计算器打）；
3. 只对最有希望的若干组合回调 calculate-price 做真实核价，
   从而同时拿到「菜单价」和「实付价」的差额。

用法示例：
    export MCD_MCP_TOKEN=<你的 Token>
    python3 scripts/optimizer.py --city 上海 --keyword 人民广场 \
        --people 2 --budget 60 --kcal 700 --avoid 花生 --top 3
"""

from __future__ import annotations

import argparse
import itertools
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import mcd_mcp  # noqa: E402
from nutrition import NutritionIndex, parse_nutrition_text  # noqa: E402

# ---------------------------------------------------------------- 数据结构


@dataclass
class Candidate:
    code: str
    name: str
    price: float  # 单位：元
    original_price: float
    kcal: float | None
    protein: float
    fat: float
    carb: float
    sodium: float
    matched: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)

    @property
    def kcal_per_yuan(self) -> float:
        return (self.kcal or 0) / self.price if self.price else 0.0

    @property
    def is_main(self) -> bool:
        """是否算「主食」：汉堡/卷/饭/早餐麦满分/大块肉类。"""
        return bool(MAIN_FOOD_RE.search(self.name)) or any(
            MAIN_CATEGORY_RE.search(c) for c in self.categories
        )


MAIN_FOOD_RE = re.compile(r"(堡|卷|饭|麦满分|巨无霸|麦香鸡|麦香鱼|鸡腿|鸡排|牛堡|那么大|套餐|件套)")
MAIN_CATEGORY_RE = re.compile(r"(汉堡|卷|饭|主食|早餐|牛肉|鸡肉|单人餐)")


@dataclass
class Plan:
    items: list[tuple[Candidate, int]]
    estimate: float
    actual: float | None
    original: float | None
    discount: float | None
    kcal: float
    protein: float
    sodium: float

    @property
    def label(self) -> str:
        return " + ".join(f"{c.name}×{q}" for c, q in self.items)


# ---------------------------------------------------------------- 数据获取


def resolve_store(city: str, keyword: str, store_code: str | None, be_type: int) -> dict:
    """定位门店：优先使用显式 storeCode，否则按城市+关键词搜索。"""
    if store_code:
        return {"storeCode": store_code, "storeName": f"门店 {store_code}"}
    data = mcd_mcp.call("query-nearby-stores", {"beType": be_type, "searchType": 2, "city": city, "keyword": keyword})
    stores = data.get("data") if isinstance(data, dict) else data
    if isinstance(stores, list) and stores:
        return stores[0]
    raise mcd_mcp.McdMcpError("没有找到可用门店，请换一个关键词，或用 --store-code 直接指定。")


def fetch_menu(store_code: str, be_type: int, order_type: int, be_code: str | None) -> dict:
    args = {"storeCode": store_code, "beType": be_type, "orderType": order_type}
    if be_code:
        args["beCode"] = be_code
    return mcd_mcp.call("query-meals", args)


def fetch_nutrition() -> NutritionIndex:
    data = mcd_mcp.call("list-nutrition-foods", {}, cache_ttl=12 * 3600)
    rows = parse_nutrition_text(data.get("_text", "") if isinstance(data, dict) else data)
    if not rows:
        raise mcd_mcp.McdMcpError("营养数据解析为空，请检查 MCP 返回。")
    return NutritionIndex(rows)


# ---------------------------------------------------------------- 候选构建


def build_candidates(menu: dict, index: NutritionIndex, avoid: list[str]) -> list[Candidate]:
    """把 MCP 菜单 + 营养表合成候选池。"""
    meals = (menu or {}).get("meals", {})
    cat_map: dict[str, list[str]] = {}
    for category in (menu or {}).get("categories", []):
        cname = str(category.get("name", "")).replace("\n", "")
        for entry in category.get("meals", []):
            cat_map.setdefault(str(entry.get("code")), []).append(cname)

    out: list[Candidate] = []
    for code, meal in meals.items():
        name = str(meal.get("name", "")).strip()
        if not name:
            continue
        price = round(float(meal.get("currentPrice") or 0), 2)
        original = round(float(meal.get("originalPrice") or price), 2)
        if price <= 0:
            continue
        blob = name
        if any(word and word in blob for word in avoid):
            continue

        hits = index.match_many(name) or ([index.match(name)] if index.match(name) else [])
        hits = [h for h in hits if h]
        if hits:
            kcal = round(sum(h.kcal for h in hits), 1)
            protein = round(sum(h.protein for h in hits), 1)
            fat = round(sum(h.fat for h in hits), 1)
            carb = round(sum(h.carb for h in hits), 1)
            sodium = round(sum(h.sodium for h in hits), 1)
        else:
            kcal, protein, fat, carb, sodium = None, 0.0, 0.0, 0.0, 0.0

        out.append(
            Candidate(
                code=code,
                name=name,
                price=price,
                original_price=original,
                kcal=kcal,
                protein=protein,
                fat=fat,
                carb=carb,
                sodium=sodium,
                matched=[h.name for h in hits],
                categories=cat_map.get(code, []),
            )
        )
    return out


def expand_by_combo(
    cand: Candidate,
    store_code: str,
    be_type: int,
    order_type: int,
    be_code: str | None,
    index: NutritionIndex,
) -> bool:
    """对营养表里匹配不上的餐品，用 query-meal-detail 反查套餐默认组成再累加营养。

    例如「精选单人餐三件套」→ 主食(巨无霸) + 小食 + 饮料，逐项匹配营养表求和。
    """
    args = {"storeCode": store_code, "beType": be_type, "orderType": order_type, "code": cand.code}
    if be_code:
        args["beCode"] = be_code
    try:
        data = mcd_mcp.raw_call("query-meal-detail", args)
    except mcd_mcp.McdMcpError:
        return False

    total = {"kcal": 0.0, "protein": 0.0, "fat": 0.0, "carb": 0.0, "sodium": 0.0}
    hits: list[str] = []
    for rnd in data.get("rounds", []) or []:
        choices = rnd.get("choices", []) or []
        picked = [c for c in choices if (c.get("quantity") or 0) > 0]
        if not picked:
            picked = [c for c in choices if c.get("isDefault") == 1]
        for choice in picked:
            name = str(choice.get("name", "")).strip()
            qty = int(choice.get("quantity") or 1)
            hit = index.match(name)
            if hit:
                hits.append(f"{name}×{qty}")
                total["kcal"] += hit.kcal * qty
                total["protein"] += hit.protein * qty
                total["fat"] += hit.fat * qty
                total["carb"] += hit.carb * qty
                total["sodium"] += hit.sodium * qty
    if not hits:
        return False
    cand.kcal = round(total["kcal"], 1)
    cand.protein = round(total["protein"], 1)
    cand.fat = round(total["fat"], 1)
    cand.carb = round(total["carb"], 1)
    cand.sodium = round(total["sodium"], 1)
    cand.matched = [f"套餐组成：{', '.join(hits)}"]
    return True


def select_pool(candidates: list[Candidate], size: int) -> list[Candidate]:
    """分层采样：主食保底占 2/3，其余留给小食饮料，避免候选池全是便宜甜品。"""
    mains = [c for c in candidates if c.is_main]
    others = [c for c in candidates if not c.is_main]
    mains.sort(key=lambda c: -c.kcal_per_yuan)
    others.sort(key=lambda c: -c.kcal_per_yuan)

    n_main = max(1, int(size * 2 / 3))
    pool = mains[:n_main] + others[: size - n_main]
    if len(pool) < size:  # 某一类不够，用另一类补齐
        filler = (mains + others)
        for cand in filler:
            if len(pool) >= size:
                break
            if cand not in pool:
                pool.append(cand)
    return pool


# ---------------------------------------------------------------- 组合搜索


def enumerate_plans(
    pool: list[Candidate],
    people: int,
    budget: float,
    kcal_limit: float | None,
    min_kcal: float | None,
    require_main: int,
    limit: int,
) -> list[Plan]:
    """枚举「给 people 个人各点一份」的多重集合组合，并按约束剪枝。

    people 较大时组合数爆炸，这里用候选池截断 + 扫描上限兜底。
    """
    total_kcal_cap = kcal_limit * people if kcal_limit else None
    total_kcal_floor = min_kcal * people if min_kcal else None
    combos = itertools.combinations_with_replacement(range(len(pool)), people)
    plans: list[Plan] = []
    scanned = 0
    for combo in combos:
        scanned += 1
        if scanned > 400_000:  # 兜底，避免极端参数卡死
            break
        counts: dict[int, int] = {}
        for idx in combo:
            counts[idx] = counts.get(idx, 0) + 1

        if require_main and sum(q for i, q in counts.items() if pool[i].is_main) < require_main:
            continue
        estimate = round(sum(pool[i].price * q for i, q in counts.items()), 2)
        if budget and estimate > budget:
            continue
        kcal = sum((pool[i].kcal or 0) * q for i, q in counts.items())
        if total_kcal_cap and kcal > total_kcal_cap:
            continue
        if total_kcal_floor and kcal < total_kcal_floor:
            continue
        protein = sum(pool[i].protein * q for i, q in counts.items())
        sodium = sum(pool[i].sodium * q for i, q in counts.items())
        plans.append(
            Plan(
                items=[(pool[i], q) for i, q in sorted(counts.items())],
                estimate=estimate,
                actual=None,
                original=None,
                discount=None,
                kcal=round(kcal, 1),
                protein=round(protein, 1),
                sodium=round(sodium, 1),
            )
        )
    plans.sort(key=lambda p: p.estimate)
    return plans[:limit]


def price_check(plans: list[Plan], store_code: str, be_type: int, order_type: int, be_code: str | None, limit: int) -> None:
    """对最有希望的组合回调 MCP 做真实核价（价格单位为分）。"""
    for plan in plans[:limit]:
        args = {
            "storeCode": store_code,
            "beType": be_type,
            "orderType": order_type,
            "items": [{"productCode": c.code, "quantity": q} for c, q in plan.items],
        }
        if be_code:
            args["beCode"] = be_code
        try:
            data = mcd_mcp.raw_call("calculate-price", args)
        except mcd_mcp.McdMcpError:
            continue
        # 接口金额单位为分：price=应付，originalPrice=原价，discount=优惠额
        plan.actual = round(float(data.get("price", 0)) / 100, 2)
        plan.original = round(float(data.get("originalPrice", data.get("price", 0))) / 100, 2)
        plan.discount = round(float(data.get("discount", 0)) / 100, 2)
        if plan.discount <= 0 and plan.original and plan.actual:
            plan.discount = round(plan.original - plan.actual, 2)


# ---------------------------------------------------------------- 评分与输出


def balanced_score(plan: Plan, kcal_target: float) -> float:
    """营养均衡分：蛋白供能比适中、钠不过量、热量贴近目标。"""
    if plan.kcal <= 0:
        return 0.0
    protein_ratio = plan.protein * 4 / plan.kcal
    ratio_score = 1 - abs(protein_ratio - 0.20) * 3  # 蛋白供能 20% 附近最佳
    sodium_score = 1 - min(plan.sodium / (2000 * max(len(plan.items), 1)), 1) * 0.5
    kcal_score = 1 - min(abs(plan.kcal - kcal_target) / kcal_target, 1) * 0.5
    return round(max(ratio_score, 0) * 0.4 + sodium_score * 0.3 + kcal_score * 0.3, 4)


def pick(plans: list[Plan], key, n: int) -> list[Plan]:
    return sorted(plans, key=key, reverse=True)[:n]


def render(plan: Plan, title: str, people: int) -> str:
    paid = plan.actual if plan.actual is not None else plan.estimate
    original = plan.original if plan.original is not None else plan.estimate
    saved = max(plan.discount or 0, round(original - paid, 2))
    lines = [f"【{title}】{plan.label}"]
    lines.append(
        f"  实付 ¥{paid:.2f}"
        + (f"（原价 ¥{original:.2f}，省 ¥{saved:.2f}）" if saved > 0 else f"（原价 ¥{original:.2f}）")
        + f"｜人均 ¥{paid / max(people, 1):.2f}"
    )
    lines.append(
        f"  热量 {plan.kcal:.0f} kcal（人均 {plan.kcal / max(people, 1):.0f}）"
        f"｜蛋白 {plan.protein:.0f} g｜钠 {plan.sodium:.0f} mg"
    )
    for cand, qty in plan.items:
        kcal_txt = f"{cand.kcal:.0f} kcal" if cand.kcal is not None else "热量未知"
        lines.append(f"    - {cand.name} ×{qty}  ¥{cand.price:.2f}  {kcal_txt}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="麦麦最优解：多约束麦当劳点餐求解器")
    parser.add_argument("--city", default="上海", help="城市，用于搜索门店")
    parser.add_argument("--keyword", default="人民广场", help="位置关键词")
    parser.add_argument("--store-code", default=None, help="直接指定门店编码")
    parser.add_argument("--be-type", type=int, default=1, help="1-到店自取 2-麦乐送 5-得来速 6-团餐")
    parser.add_argument("--people", type=int, default=1, help="用餐人数")
    parser.add_argument("--budget", type=float, default=0, help="总预算上限（元），0 表示不限")
    parser.add_argument("--kcal", type=float, default=0, help="每人热量上限（kcal），0 表示不限")
    parser.add_argument("--min-kcal", type=float, default=0, help="每人热量下限（kcal），默认取上限的 60%")
    parser.add_argument("--avoid", default="", help="忌口关键词，逗号分隔")
    parser.add_argument("--pool", type=int, default=24, help="候选餐品池大小")
    parser.add_argument("--expand", type=int, default=40, help="用套餐组成反推营养的最大调用次数")
    parser.add_argument("--main", type=int, default=0, help="主食件数下限，默认等于人数")
    parser.add_argument("--check", type=int, default=24, help="回调 MCP 精算的组合数量")
    parser.add_argument("--top", type=int, default=1, help="每种目标输出的方案数")
    parser.add_argument("--json", action="store_true", help="额外输出 JSON，便于二次渲染")
    args = parser.parse_args()

    order_type = 1 if args.be_type in (1, 5) else 2

    try:
        store = resolve_store(args.city, args.keyword, args.store_code, args.be_type)
        store_code = store["storeCode"]
        be_code = store.get("beCode") or None
        menu = fetch_menu(store_code, args.be_type, order_type, be_code)
        index = fetch_nutrition()
    except mcd_mcp.McdMcpError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1

    avoid = [w.strip() for w in args.avoid.split("，") if w.strip()] + [
        w.strip() for w in args.avoid.split(",") if w.strip()
    ]
    pool_all = build_candidates(menu, index, avoid)

    # 营养表里查不到的餐品，用套餐组成反推（限次调用，避免打爆限流）
    unknown = [c for c in pool_all if c.kcal is None]
    unknown.sort(key=lambda c: (not c.is_main, c.price))
    expanded = 0
    for cand in unknown[: args.expand]:
        if expand_by_combo(cand, store_code, args.be_type, order_type, be_code, index):
            expanded += 1

    with_kcal = [c for c in pool_all if c.kcal is not None]
    pool = select_pool(with_kcal or pool_all, args.pool)

    kcal_limit = args.kcal if args.kcal > 0 else None
    min_kcal = args.min_kcal if args.min_kcal > 0 else (kcal_limit * 0.6 if kcal_limit else 300)
    require_main = args.main if args.main > 0 else args.people
    if not any(c.is_main for c in pool):
        require_main = 0  # 候选池里没有主食，别把解空间剪没了

    plans = enumerate_plans(pool, args.people, args.budget, kcal_limit, min_kcal, require_main, args.check)
    if not plans:
        print("✗ 当前约束下无解，试试放宽预算、热量下限或主食件数。", file=sys.stderr)
        return 2

    price_check(plans, store_code, args.be_type, order_type, be_code, args.check)
    priced = [p for p in plans if p.actual is not None] or plans

    kcal_target = (kcal_limit or 700) * args.people * 0.85
    chosen: set[str] = set()

    def take(ranked: list[Plan]) -> Plan | None:
        for plan in ranked:
            if plan.label not in chosen:
                chosen.add(plan.label)
                return plan
        return ranked[0] if ranked else None

    cheapest = [p for p in (take(pick(priced, lambda p: -(p.actual if p.actual is not None else p.estimate), args.top * 3)) for _ in range(args.top)) if p]
    fullest = [p for p in (take(pick(priced, lambda p: (p.kcal / (p.actual or p.estimate or 1)), args.top * 3)) for _ in range(args.top)) if p]
    balanced = [p for p in (take(pick(priced, lambda p: balanced_score(p, kcal_target), args.top * 3)) for _ in range(args.top)) if p]

    header = (
        f"门店：{store.get('storeName', store_code)}（{store_code}）｜"
        f"{args.people} 人｜预算 {'¥%.0f' % args.budget if args.budget else '不限'}｜"
        f"人均热量上限 {int(kcal_limit) if kcal_limit else '不限'} kcal"
    )
    print(header)
    print("=" * len(header))
    for idx in range(args.top):
        if idx < len(cheapest):
            print(render(cheapest[idx], "最省钱", args.people))
        if idx < len(fullest):
            print(render(fullest[idx], "最扛饿", args.people))
        if idx < len(balanced):
            print(render(balanced[idx], "最均衡", args.people))
        print("-" * len(header))

    if args.json:
        payload = []
        for title, group in (("最省钱", cheapest), ("最扛饿", fullest), ("最均衡", balanced)):
            for plan in group:
                payload.append(
                    {
                        "target": title,
                        "items": [{"name": c.name, "code": c.code, "qty": q} for c, q in plan.items],
                        "estimate": plan.estimate,
                        "actual": plan.actual,
                        "kcal": plan.kcal,
                        "protein": plan.protein,
                        "sodium": plan.sodium,
                    }
                )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
