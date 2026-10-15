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
class Coupon:
    """一张可在当前门店使用的券。"""

    coupon_id: str
    coupon_code: str
    title: str
    product_codes: set[str] = field(default_factory=set)
    deadline: str = ""

    def applies_to(self, code: str) -> bool:
        return code in self.product_codes


@dataclass
class Candidate:
    code: str
    name: str
    price: float  # 预算剪枝用的「到手价」：券商品用的是带券实付价
    original_price: float
    kcal: float | None
    protein: float
    fat: float
    carb: float
    sodium: float
    matched: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    coupon: Coupon | None = None  # 该商品可叠加的券（券商品一定有）
    is_coupon_item: bool = False  # 券专属商品：菜单里买不到，只能凭券下单
    coupon_price: float | None = None  # 带券核价得到的实付（元）
    coupon_original: float | None = None  # 带券核价得到的原价（元）

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
BUY_ONE_GET_ONE_RE = re.compile(r"(买一送一|买一赠一|第二份|双人份|双份)")


@dataclass
class Plan:
    items: list[tuple[Candidate, int]]
    estimate: float  # 菜单价合计（预算剪枝用）
    actual: float | None  # 不带券的真实实付
    original: float | None  # 不带券的接口原价
    discount: float | None  # 不带券的优惠额
    kcal: float
    protein: float
    sodium: float
    coupon_actual: float | None = None  # 叠加券后的真实实付
    coupon_discount: float | None = None  # 叠加券后的总优惠额
    used_coupons: list[str] = field(default_factory=list)

    @property
    def paid(self) -> float:
        """最终到手价：有券价用券价，否则用不带券价，都没有才回落到菜单价。"""
        for value in (self.coupon_actual, self.actual):
            if value is not None and value > 0:
                return value
        return self.estimate

    @property
    def list_price(self) -> float:
        """用于展示的原价基准：优先接口原价，其次菜单价。"""
        return self.original if self.original else self.estimate

    @property
    def saved(self) -> float:
        return max(round(self.list_price - self.paid, 2), 0.0)

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
    text = data if isinstance(data, str) else (data or {}).get("_text", "")
    rows = parse_nutrition_text(text)
    if not rows:
        raise mcd_mcp.McdMcpError("营养数据解析为空，请检查 MCP 返回。")
    return NutritionIndex(rows)


# ---------------------------------------------------------------- 优惠券链路


def claim_coupons(verbose: bool = True) -> int:
    """一键领取当前可领的麦麦省券，返回领取成功的张数。

    这是真实写操作（券会进你的卡包），因此单独成函数、失败不中断主流程。
    """
    ok, data, err = mcd_mcp.safe_call("auto-bind-coupons", {})
    if not ok:
        if verbose:
            hint = "券已领过或当前没有可领活动" if "暂无" in err else err
            print(f"· 领券跳过：{hint}", file=sys.stderr)
        return 0
    got = (data or {}).get("successCoupons") or []
    if verbose:
        print(f"· 已一键领取 {len(got)} 张麦麦省券" if got else "· 券已领过，无可领新券", file=sys.stderr)
    return len(got)


def load_store_coupons(store_code: str, be_type: int, order_type: int, be_code: str | None) -> list[Coupon]:
    """查询该门店当前可用的券，以及每张券对应的商品编码。

    注意：券对应的商品可能不在菜单里（例如「中薯条买一送一」是券专属商品），
    这类商品不带券核价会返回 price=0，必须凭券下单。
    """
    args = {"storeCode": store_code, "beType": be_type, "orderType": order_type}
    if be_code:
        args["beCode"] = be_code
    ok, data, _ = mcd_mcp.safe_call("query-store-coupons", args)
    if not ok or not isinstance(data, list):
        return []

    coupons: list[Coupon] = []
    for raw in data:
        codes = {str(p.get("productCode")) for p in (raw.get("products") or []) if p.get("productCode")}
        if not codes:
            continue
        coupons.append(
            Coupon(
                coupon_id=str(raw.get("couponId", "")),
                coupon_code=str(raw.get("couponCode", "")),
                title=str(raw.get("title") or raw.get("couponName") or "优惠券"),
                product_codes=codes,
                deadline=str(raw.get("tradeDateTime", "")),
            )
        )
    return coupons


def price_with_coupons(items: list[dict], store_code: str, be_type: int, order_type: int, be_code: str | None) -> dict:
    args = {"storeCode": store_code, "beType": be_type, "orderType": order_type, "items": items}
    if be_code:
        args["beCode"] = be_code
    return mcd_mcp.raw_call("calculate-price", args)


def build_coupon_candidates(
    coupons: list[Coupon],
    menu_codes: set[str],
    store_code: str,
    be_type: int,
    order_type: int,
    be_code: str | None,
    index: NutritionIndex,
) -> list[Candidate]:
    """为「菜单里买不到的券专属商品」单独建模：带券核价拿真实价格。

    例如「中薯条买一送一」原价 ¥27、凭券 ¥13.5，但它不在菜单里，
    不建模就永远进不了候选池——而这恰恰是最省钱方案的关键。
    """
    out: list[Candidate] = []
    seen: set[str] = set()
    for coupon in coupons:
        for code in sorted(coupon.product_codes):
            if code in menu_codes or code in seen:
                continue
            seen.add(code)
            try:
                data = price_with_coupons(
                    [{"productCode": code, "quantity": 1, "couponId": coupon.coupon_id, "couponCode": coupon.coupon_code}],
                    store_code,
                    be_type,
                    order_type,
                    be_code,
                )
            except mcd_mcp.McdMcpError:
                continue
            paid = round(float(data.get("price") or 0) / 100, 2)
            if paid <= 0:  # 券已失效或商品不可售
                continue
            original = round(float(data.get("originalPrice") or paid * 100) / 100, 2)

            name = coupon.title
            hits = index.match_many(name) or ([index.match(name)] if index.match(name) else [])
            hits = [h for h in hits if h]
            # 「买一送一」类券拿到的是两份，营养要翻倍才是真实摄入
            multiplier = 2 if BUY_ONE_GET_ONE_RE.search(name) else 1
            out.append(
                Candidate(
                    code=code,
                    name=f"{name}（券）",
                    price=paid,
                    original_price=original,
                    kcal=round(sum(h.kcal for h in hits) * multiplier, 1) if hits else None,
                    protein=round(sum(h.protein for h in hits) * multiplier, 1),
                    fat=round(sum(h.fat for h in hits) * multiplier, 1),
                    carb=round(sum(h.carb for h in hits) * multiplier, 1),
                    sodium=round(sum(h.sodium for h in hits) * multiplier, 1),
                    matched=[f"{h.name}×{multiplier}" for h in hits],
                    categories=["优惠券专享"],
                    coupon=coupon,
                    is_coupon_item=True,
                    coupon_price=paid,
                    coupon_original=original,
                )
            )
    return out


# ---------------------------------------------------------------- 候选构建


def build_candidates(
    menu: dict, index: NutritionIndex, avoid: list[str], coupons: list[Coupon] | None = None
) -> list[Candidate]:
    """把 MCP 菜单 + 营养表 + 可用券合成候选池。"""
    coupons = coupons or []
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

        # 该商品是否有可叠加的券（例如「9.9 元中杯冰美式」）
        coupon = next((cp for cp in coupons if cp.applies_to(code)), None)

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
                coupon=coupon,
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
        for cand in mains + others:
            if len(pool) >= size:
                break
            if cand not in pool:
                pool.append(cand)
    return pool


def inject_coupon_items(pool: list[Candidate], coupon_items: list[Candidate], size: int) -> list[Candidate]:
    """券专属商品强制入池 —— 它们带着确定性折扣，最省钱方案几乎必然包含。"""
    merged = list(coupon_items)
    for cand in pool:
        if len(merged) >= size:
            break
        if all(c.code != cand.code for c in merged):
            merged.append(cand)
    return merged[:size]


# ---------------------------------------------------------------- 组合搜索


def enumerate_plans(
    pool: list[Candidate],
    people: int,
    budget: float,
    kcal_limit: float | None,
    min_kcal: float | None,
    require_main: int,
    extra: int = 1,
) -> list[Plan]:
    """枚举「每人一份主食保底 + 最多 extra 件共享附加」的多重集合组合。

    这里刻意允许件数 > 人数：真实点餐本来就是「两个主食 + 一份共享小食」，
    而券商品（如买一送一的薯条）大多是附加项——如果只枚举恰好 people 件，
    它们永远挤不进方案，券就等于白领了。

    people 较大时组合数爆炸，这里用候选池截断 + 扫描上限兜底。
    返回全部可行解，交给 select_diverse 按目标挑选，避免提前砍掉最优解。
    """
    total_kcal_cap = kcal_limit * people if kcal_limit else None
    total_kcal_floor = min_kcal * people if min_kcal else None
    max_items = people + max(extra, 0)
    plans: list[Plan] = []
    scanned = 0
    for size in range(people, max_items + 1):
        combos = itertools.combinations_with_replacement(range(len(pool)), size)
        for combo in combos:
            scanned += 1
            if scanned > 600_000:  # 兜底，避免极端参数卡死
                return plans
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
    return plans


def select_diverse(plans: list[Plan], kcal_target: float, budget: int) -> list[Plan]:
    """按三个目标各自保留头部候选再合并。

    关键点：如果先按价格排序砍到 N 个再核价，「最扛饿」「最均衡」就只能在
    便宜方案里矮子里拔将军。这里让每个目标先各自用自己的代理指标排一遍，
    各自贡献 1/3 名额，最后再统一核价，才拿得到真正的三套最优解。
    """
    per_target = max(1, budget // 3)
    chosen: dict[str, Plan] = {}

    def add(ranked: list[Plan]) -> None:
        for plan in ranked[:per_target]:
            chosen.setdefault(plan.label, plan)

    add(sorted(plans, key=lambda p: p.estimate))  # 代理：便宜
    add(sorted(plans, key=lambda p: -(p.kcal / (p.estimate or 1))))  # 代理：每元热量
    add(sorted(plans, key=lambda p: -balanced_score(p, kcal_target)))  # 代理：营养均衡

    ordered = sorted(chosen.values(), key=lambda p: p.estimate)
    return ordered[:budget]


def price_check(
    plans: list[Plan], store_code: str, be_type: int, order_type: int, be_code: str | None, limit: int
) -> None:
    """对最有希望的组合回调 MCP 做真实核价（价格单位为分）。

    每个方案核两次：一次不带券、一次尽量挂券，取到手价更低的一次，
    这样「省了多少」才是真实可复现的数字，而不是估算。
    """
    for plan in plans[:limit]:
        plain_items = [{"productCode": c.code, "quantity": q} for c, q in plan.items]
        coupon_items = []
        used: list[str] = []
        for cand, qty in plan.items:
            item = {"productCode": cand.code, "quantity": qty}
            if cand.coupon:
                item["couponId"] = cand.coupon.coupon_id
                item["couponCode"] = cand.coupon.coupon_code
                note = cand.coupon.title if cand.is_coupon_item else f"{cand.coupon.title}（{cand.name}×{qty}）"
                if note not in used:
                    used.append(note)
            coupon_items.append(item)

        def quote(items: list[dict]) -> tuple[float, float, float] | None:
            """核价一次。返回 (实付, 原价, 优惠额)，报价不可信时返回 None。"""
            try:
                data = price_with_coupons(items, store_code, be_type, order_type, be_code)
            except mcd_mcp.McdMcpError:
                return None
            paid = round(float(data.get("price") or 0) / 100, 2)
            if paid <= 0:
                return None  # 整单不可售，别让 0 元混入最优解
            # 关键：券专属商品不带券时会被接口静默丢弃，只算其余商品。
            # 这种报价便宜得虚假，照单下单根本加不进薯条，所以直接判无效。
            listed = data.get("productList") or []
            if isinstance(listed, list) and listed and len(listed) < len(items):
                return None
            original = round(float(data.get("originalPrice") or 0) / 100, 2)
            discount = round(float(data.get("discount") or 0) / 100, 2)
            if discount <= 0 and original:
                discount = round(original - paid, 2)
            return paid, original, discount

        has_coupon_only = any(c.is_coupon_item for c, _ in plan.items)
        has_coupon = any(c.coupon for c, _ in plan.items)

        # 含券专属商品的方案只有「带券」这一条路可走，不带券的报价必然缺件
        plain = None if has_coupon_only else quote(plain_items)
        if plain:
            plan.actual, plan.original, plan.discount = plain

        if has_coupon:
            with_coupon = quote(coupon_items)
            if with_coupon:
                paid, original, discount = with_coupon
                if plan.actual is None or paid < plan.actual:
                    plan.coupon_actual = paid
                    plan.coupon_discount = discount
                    plan.used_coupons = used
                if not plan.original:
                    plan.original = original


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
    paid = plan.paid
    per_person = paid / max(people, 1)
    lines = [f"【{title}】{plan.label}"]
    head = f"  实付 ¥{paid:.2f}｜人均 ¥{per_person:.2f}"
    if plan.saved > 0:
        pct = plan.saved / plan.list_price * 100 if plan.list_price else 0
        head += f"｜省 ¥{plan.saved:.2f}（原价 ¥{plan.list_price:.2f}，省 {pct:.0f}%）"
    lines.append(head)
    lines.append(
        f"  热量 {plan.kcal:.0f} kcal（人均 {plan.kcal / max(people, 1):.0f}）"
        f"｜蛋白 {plan.protein:.0f} g｜钠 {plan.sodium:.0f} mg"
        + (f"｜性价比 {plan.kcal / paid:.0f} kcal/元" if paid else "")
    )
    for cand, qty in plan.items:
        kcal_txt = f"{cand.kcal:.0f} kcal" if cand.kcal is not None else "热量未知"
        price_txt = f"¥{cand.price:.2f}"
        if cand.is_coupon_item and cand.coupon_original:
            price_txt += f"（原价 ¥{cand.coupon_original:.2f}）"
        lines.append(f"    - {cand.name} ×{qty}  {price_txt}  {kcal_txt}")
    if plan.used_coupons:
        lines.append(f"    ⓘ 用券：{'；'.join(plan.used_coupons)}")
    return "\n".join(lines)


def plan_payload(plan: Plan, target: str, people: int) -> dict:
    paid = plan.paid
    return {
        "target": target,
        "items": [
            {
                "name": c.name,
                "code": c.code,
                "qty": q,
                "price": c.price,
                "kcal": c.kcal,
                "coupon": c.coupon.title if c.coupon else None,
            }
            for c, q in plan.items
        ],
        "estimate": plan.estimate,
        "actual": plan.actual,
        "coupon_actual": plan.coupon_actual,
        "paid": paid,
        "list_price": plan.list_price,
        "saved": plan.saved,
        "per_person": round(paid / max(people, 1), 2),
        "kcal": plan.kcal,
        "protein": plan.protein,
        "sodium": plan.sodium,
        "used_coupons": plan.used_coupons,
    }


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>麦麦最优解 · 点餐方案</title>
<style>
  :root { --ink:#12141a; --sub:#6b7280; --line:#e8eaee; --bg:#f6f7f9;
          --gold:#ffc72c; --red:#da291c; --green:#0f9d58; --card:#ffffff; }
  * { box-sizing: border-box; }
  body { margin:0; padding:32px 16px; background:var(--bg); color:var(--ink);
         font:15px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif; }
  .wrap { max-width:860px; margin:0 auto; }
  .hero { background:linear-gradient(135deg,#da291c,#ff6a00); color:#fff; border-radius:18px;
          padding:24px 28px; margin-bottom:20px; }
  .hero h1 { margin:0 0 6px; font-size:24px; letter-spacing:.5px; }
  .hero p { margin:0; opacity:.92; font-size:13px; }
  .badges { margin-top:14px; display:flex; flex-wrap:wrap; gap:8px; }
  .badge { background:rgba(255,255,255,.18); border:1px solid rgba(255,255,255,.3);
           border-radius:999px; padding:4px 12px; font-size:12px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:16px;
          padding:20px 22px; margin-bottom:14px; }
  .card h2 { margin:0 0 14px; font-size:17px; display:flex; align-items:center; gap:8px; }
  .tag { font-size:12px; font-weight:600; padding:3px 10px; border-radius:6px; color:#fff; }
  .t-cheap { background:var(--red); } .t-full { background:#f0932b; } .t-balance { background:var(--green); }
  .price { font-size:30px; font-weight:700; line-height:1.1; }
  .price small { font-size:14px; font-weight:500; color:var(--sub); }
  .save { color:var(--green); font-weight:600; }
  .metrics { display:flex; flex-wrap:wrap; gap:18px; margin:14px 0 4px; font-size:13px; color:var(--sub); }
  .metrics b { color:var(--ink); font-size:15px; }
  ul.items { list-style:none; margin:14px 0 0; padding:0; border-top:1px dashed var(--line); }
  ul.items li { display:flex; justify-content:space-between; gap:12px; padding:9px 0;
                border-bottom:1px dashed var(--line); font-size:14px; }
  ul.items .nm { flex:1; }
  ul.items .kcal { color:var(--sub); font-size:13px; }
  .coupon { margin-top:12px; background:#fff8e1; border-left:3px solid var(--gold);
            padding:9px 12px; border-radius:0 8px 8px 0; font-size:13px; }
  footer { text-align:center; color:var(--sub); font-size:12px; margin-top:24px; }
</style>
</head>
<body><div class="wrap">
<div class="hero">
  <h1>麦麦最优解</h1>
  <p>门店 __STORE_NAME__（__STORE_CODE__）｜__PEOPLE__ 人｜预算 __BUDGET__｜人均热量上限 __KCAL__</p>
  <div class="badges">__BADGES__</div>
</div>
__CARDS__
<footer>由麦当劳 MCP 的真实菜单 · 价格 · 营养 · 优惠券数据求解生成</footer>
</div></body></html>
"""


def render_html(meta: dict, plans: list[dict]) -> str:
    """把方案渲染成一张可直接分享的 HTML 卡片。"""
    tag_class = {"最省钱": "t-cheap", "最扛饿": "t-full", "最均衡": "t-balance"}
    cards = []
    for plan in plans:
        rows = []
        for item in plan["items"]:
            kcal = f'{item["kcal"]:.0f} kcal' if item.get("kcal") else "热量未知"
            rows.append(
                f'<li><span class="nm">{item["name"]} ×{item["qty"]}</span>'
                f'<span class="kcal">¥{item["price"]:.2f} · {kcal}</span></li>'
            )
        save_html = (
            f'<span class="save">省 ¥{plan["saved"]:.2f}</span>（原价 ¥{plan["list_price"]:.2f}）'  # noqa: E501
            if plan["saved"] > 0
            else f'<span style="color:var(--sub)">原价 ¥{plan["list_price"]:.2f}</span>'
        )
        coupon_html = ""
        if plan["used_coupons"]:
            coupon_html = f'<div class="coupon">用券：{"；".join(plan["used_coupons"])}</div>'
        cards.append(
            f'''<div class="card">
  <h2><span class="tag {tag_class.get(plan["target"], "t-cheap")}">{plan["target"]}</span></h2>
  <div class="price">¥{plan["paid"]:.2f} <small>人均 ¥{plan["per_person"]:.2f}</small> · {save_html}</div>
  <div class="metrics">
    <span>热量 <b>{plan["kcal"]:.0f}</b> kcal</span>
    <span>蛋白 <b>{plan["protein"]:.0f}</b> g</span>
    <span>钠 <b>{plan["sodium"]:.0f}</b> mg</span>
    <span>性价比 <b>{plan["kcal"] / plan["paid"]:.0f}</b> kcal/元</span>
  </div>
  <ul class="items">{"".join(rows)}</ul>{coupon_html}
</div>'''
        )

    badges = [f'<span class="badge">{c["title"]}</span>' for c in meta.get("coupons", [])]
    if not badges:
        badges = ['<span class="badge">当前门店无可叠加券</span>']
    # 模板里有大量 CSS 花括号，不能用 str.format，这里做纯占位符替换
    html = HTML_TEMPLATE
    for token, value in (
        ("__STORE_NAME__", meta["store"]["name"]),
        ("__STORE_CODE__", meta["store"]["code"]),
        ("__PEOPLE__", str(meta["people"])),
        ("__BUDGET__", f'¥{meta["budget"]:.0f}' if meta.get("budget") else "不限"),
        ("__KCAL__", f'{meta["kcal_limit"]:.0f} kcal' if meta.get("kcal_limit") else "不限"),
        ("__BADGES__", "".join(badges)),
        ("__CARDS__", "\n".join(cards)),
    ):
        html = html.replace(token, value)
    return html


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
    parser.add_argument("--extra", type=int, default=1, help="除主食外允许的共享附加件数，默认 1")
    parser.add_argument("--check", type=int, default=24, help="回调 MCP 精算的组合数量")
    parser.add_argument("--top", type=int, default=1, help="每种目标输出的方案数")
    parser.add_argument("--no-claim", action="store_true", help="跳过一键领券，只用现有卡包里的券")
    parser.add_argument("--json", action="store_true", help="额外输出 JSON，便于二次渲染")
    parser.add_argument("--html", default=None, help="把方案渲染成一份可直接分享的 HTML 卡片")
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

    # ---- 券链路：先领券，再查门店可用券（领券后必须强制回源，不能读旧缓存）
    coupon_note = "未启用"
    if not args.no_claim:
        claim_coupons()
    coupons = load_store_coupons(store_code, args.be_type, order_type, be_code)
    if coupons:
        coupon_note = f"{len(coupons)} 张可用"

    avoid = [w.strip() for w in args.avoid.replace("，", ",").split(",") if w.strip()]
    pool_all = build_candidates(menu, index, avoid, coupons)

    # 菜单里买不到的券专属商品，带券核价后单独建模
    coupon_items = build_coupon_candidates(
        coupons,
        set((menu or {}).get("meals", {}).keys()),
        store_code,
        args.be_type,
        order_type,
        be_code,
        index,
    )
    for item in coupon_items:
        pool_all.append(item)

    # 营养表里查不到的餐品，用套餐组成反推（限次调用，避免打爆限流）
    unknown = [c for c in pool_all if c.kcal is None]
    unknown.sort(key=lambda c: (not c.is_main, c.price))
    for cand in unknown[: args.expand]:
        expand_by_combo(cand, store_code, args.be_type, order_type, be_code, index)

    with_kcal = [c for c in pool_all if c.kcal is not None]
    pool = select_pool(with_kcal or pool_all, args.pool)
    pool = inject_coupon_items(pool, coupon_items, args.pool + len(coupon_items))

    kcal_limit = args.kcal if args.kcal > 0 else None
    min_kcal = args.min_kcal if args.min_kcal > 0 else (kcal_limit * 0.6 if kcal_limit else 300)
    require_main = args.main if args.main > 0 else args.people
    if not any(c.is_main for c in pool):
        require_main = 0  # 候选池里没有主食，别把解空间剪没了

    feasible = enumerate_plans(pool, args.people, args.budget, kcal_limit, min_kcal, require_main, args.extra)
    if not feasible:
        print("✗ 当前约束下无解，试试放宽预算、热量下限或主食件数。", file=sys.stderr)
        return 2

    kcal_target = (kcal_limit or 700) * args.people * 0.85
    price_check(
        select_diverse(feasible, kcal_target, args.check),
        store_code,
        args.be_type,
        order_type,
        be_code,
        args.check,
    )
    priced = [p for p in feasible if p.actual is not None or p.coupon_actual is not None] or feasible

    # 菜单价是「起价」，套餐真实核价往往更高，所以核价后必须按实付再复核一次预算
    over_budget = 0
    if args.budget:
        within = [p for p in priced if p.paid <= args.budget]
        over_budget = len(priced) - len(within)
        if within:
            priced = within

    chosen: set[str] = set()

    def take(ranked: list[Plan]) -> Plan | None:
        for plan in ranked:
            if plan.label not in chosen:
                chosen.add(plan.label)
                return plan
        return ranked[0] if ranked else None

    cheapest = [p for p in (take(pick(priced, lambda p: -p.paid, args.top * 3)) for _ in range(args.top)) if p]
    fullest = [
        p for p in (take(pick(priced, lambda p: p.kcal / (p.paid or 1), args.top * 3)) for _ in range(args.top)) if p
    ]
    balanced = [
        p for p in (take(pick(priced, lambda p: balanced_score(p, kcal_target), args.top * 3)) for _ in range(args.top))
        if p
    ]

    header = (
        f"门店：{store.get('storeName', store_code)}（{store_code}）｜"
        f"{args.people} 人｜预算 {'¥%.0f' % args.budget if args.budget else '不限'}｜"
        f"人均热量上限 {int(kcal_limit) if kcal_limit else '不限'} kcal｜券：{coupon_note}"
    )
    print(header)
    print("=" * len(header))
    print(f"可行方案 {len(feasible)} 个，实际核价 {len(priced)} 个（预算内）")
    if over_budget:
        print(f"· 已剔除 {over_budget} 个核价后超出预算的组合")
    print("-" * len(header))
    for idx in range(args.top):
        if idx < len(cheapest):
            print(render(cheapest[idx], "最省钱", args.people))
        if idx < len(fullest):
            print(render(fullest[idx], "最扛饿", args.people))
        if idx < len(balanced):
            print(render(balanced[idx], "最均衡", args.people))
        print("-" * len(header))

    if args.json or args.html:
        groups = (("最省钱", cheapest), ("最扛饿", fullest), ("最均衡", balanced))
        payload = [plan_payload(plan, title, args.people) for title, group in groups for plan in group]
        meta = {
            "store": {"code": store_code, "name": store.get("storeName", store_code)},
            "people": args.people,
            "budget": args.budget or None,
            "kcal_limit": kcal_limit,
            "coupons": [{"title": c.title, "deadline": c.deadline} for c in coupons],
            "feasible": len(feasible),
        }
        if args.json:
            print(json.dumps({"meta": meta, "plans": payload}, ensure_ascii=False, indent=2))
        if args.html:
            Path(args.html).write_text(
                render_html(meta, payload),
                encoding="utf-8",
            )
            print(f"✓ HTML 报告已生成：{args.html}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
