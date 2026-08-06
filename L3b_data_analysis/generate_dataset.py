"""L3b 夹具生成器：脏订单数据（CSV + XLSX）+ 隐藏答案。

用法：python generate_dataset.py [--reset]
产物：
    workspace/orders_raw.csv      被测 Agent 的输入（与 xlsx 内容完全一致）
    workspace/orders_raw.xlsx
    workspace/data_dictionary.md  字段说明（给 Agent）
    _ground_truth/answers.json    正确答案（勿泄露）
"""
import argparse
import csv
import json
import random
import shutil
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from mini_xlsx import write_xlsx

SEED = 20260804
ROOT = Path(__file__).resolve().parent
WS = ROOT / "workspace"
GT = ROOT / "_ground_truth"
CUTOFF = date(2026, 7, 31)

HEADER = ["order_id", "order_date", "region", "channel", "category", "product",
          "customer_id", "quantity", "unit_price", "discount_rate", "status"]

REGION_CANON = {"华东": "华东", "华东区": "华东", "east": "华东", "华 东": "华东",
                "华北": "华北", "华北区": "华北", "north": "华北",
                "华南": "华南", "华南区": "华南", "south": "华南",
                "西部": "西部", "西区": "西部", "west": "西部"}
REGION_RAW = {"华东": ["华东", "华东区", "East", "east", "华 东", " 华东 "],
              "华北": ["华北", "华北区", "North", "north "],
              "华南": ["华南", "华南区", "South"],
              "西部": ["西部", "西区", "West"]}
CATEGORIES = {
    "家电": ["空气炸锅", "扫地机器人", "破壁机"],
    "数码": ["无线耳机", "移动电源", "智能手表"],
    "家居": ["记忆棉枕", "收纳箱", "香薰机"],
    "户外": ["折叠椅", "登山杖", "保温壶"],
}
CHANNELS = ["自营商城", "第三方平台", "线下门店", "直播间"]


def fmt_date(d: date, style: int) -> str:
    return [d.isoformat(), d.strftime("%Y/%m/%d"), d.strftime("%d-%m-%Y"),
            f"{d.year}年{d.month}月{d.day}日"][style]


def fmt_money(x: float, style: int) -> str:
    return [f"{x:.2f}", f"¥{x:.2f}", f"{x:,.2f}", f" {x:.2f} "][style]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true")
    a = ap.parse_args()
    if a.reset:
        shutil.rmtree(WS, ignore_errors=True)
        shutil.rmtree(GT, ignore_errors=True)
    WS.mkdir(parents=True, exist_ok=True)
    GT.mkdir(parents=True, exist_ok=True)

    rng = random.Random(SEED)
    rows, truth = [], []          # truth 与 rows 一一对应
    seen_ids = set()
    start = date(2026, 1, 1)

    N = 400
    for i in range(1, N + 1):
        oid = f"SO{2026}{i:05d}"
        d = start + timedelta(days=rng.randrange(0, 212))
        region = rng.choice(list(REGION_RAW))
        cat = rng.choice(list(CATEGORIES))
        prod = rng.choice(CATEGORIES[cat])
        qty = rng.randrange(1, 9)
        price = round(rng.uniform(39, 899), 2)
        disc = rng.choice([0.0, 0.0, 0.05, 0.1, 0.15, 0.2, 0.3])
        cust = f"C{rng.randrange(1000, 1400)}"
        chan = rng.choice(CHANNELS)

        raw = {
            "order_id": oid,
            "order_date": fmt_date(d, rng.choice([0, 0, 0, 1, 2, 3])),
            "region": rng.choice(REGION_RAW[region]),
            "channel": chan,
            "category": cat,
            "product": prod,
            "customer_id": cust,
            "quantity": str(qty),
            "unit_price": fmt_money(price, rng.choice([0, 0, 1, 2, 3])),
            "discount_rate": f"{disc:g}",
            "status": rng.choice(["已完成", "已完成", "已完成", "已发货"]),
        }
        verdict = {"keep": True, "reason": "", "order_id": oid, "customer_id": cust,
                   "region": region, "date": d.isoformat(), "category": cat,
                   "product": prod, "quantity": qty, "unit_price": price,
                   "discount_rate": disc}

        # ---- 注入缺陷 ----
        r = rng.random()
        if i % 37 == 0:                                    # 缺失 quantity
            raw["quantity"] = rng.choice(["", "NA", "null", "-"])
            verdict.update(keep=False, reason="missing_value")
        elif i % 41 == 0:                                  # 缺失 unit_price
            raw["unit_price"] = rng.choice(["", "N/A", "未知"])
            verdict.update(keep=False, reason="missing_value")
        elif i % 43 == 0:                                  # 缺失 region -> 保留为 未知
            raw["region"] = rng.choice(["", " ", "NA"])
            verdict["region"] = "未知"
        elif i % 47 == 0:                                  # 数量异常
            q = rng.choice([0, -3, 9999, 1200])
            raw["quantity"] = str(q)
            verdict.update(keep=False, reason="outlier_quantity")
        elif i % 53 == 0:                                  # 价格异常
            p = rng.choice([-199.0, 0.0])
            raw["unit_price"] = fmt_money(p, 0)
            verdict.update(keep=False, reason="outlier_price")
        elif i % 59 == 0:                                  # 折扣异常
            dr = rng.choice([1.5, -0.2, 0.95])
            raw["discount_rate"] = f"{dr:g}"
            verdict.update(keep=False, reason="outlier_discount")
        elif i % 61 == 0:                                  # 日期非法 / 未来
            if rng.random() < 0.5:
                raw["order_date"] = rng.choice(["2026-13-05", "not a date", "0000-00-00"])
            else:
                raw["order_date"] = fmt_date(CUTOFF + timedelta(days=rng.randrange(5, 90)), 0)
            verdict.update(keep=False, reason="invalid_date")
        elif r < 0.05:                                     # 折扣缺失 -> 视为 0
            raw["discount_rate"] = ""
            verdict["discount_rate"] = 0.0

        rows.append(raw)
        truth.append(verdict)
        seen_ids.add(oid)

    # ---- 插入完全重复行（同 order_id 整行复制），副本一定排在原行之后 ----
    keepers = [i for i, t in enumerate(truth) if t["keep"]]
    for idx in sorted(rng.sample(keepers, 15), reverse=True):
        pos = rng.randrange(idx + 1, len(rows) + 1)
        rows.insert(pos, dict(rows[idx]))
        truth.insert(pos, {**truth[idx], "keep": False, "reason": "duplicate"})

    # ---- 写文件 ----
    table = [HEADER] + [[r[h] for h in HEADER] for r in rows]
    with (WS / "orders_raw.csv").open("w", encoding="utf-8-sig", newline="") as f:
        csv.writer(f).writerows(table)
    write_xlsx(WS / "orders_raw.xlsx", table, "orders")

    # ---- 计算标准答案 ----
    kept = [t for t in truth if t["keep"]]
    for t in kept:
        t["revenue"] = round(t["quantity"] * t["unit_price"] * (1 - t["discount_rate"]), 2)
    total = round(sum(t["revenue"] for t in kept), 2)
    agg = lambda key: {k: round(v, 2) for k, v in sorted(
        ((k, v) for k, v in _sum(kept, key).items()), key=lambda kv: -kv[1])}
    by_month = {k: round(v, 2) for k, v in sorted(_sum(kept, lambda t: t["date"][:7]).items())}
    prod_rev = sorted(_sum(kept, lambda t: t["product"]).items(), key=lambda kv: -kv[1])

    dropped = defaultdict(int)
    for t in truth:
        if not t["keep"]:
            dropped[t["reason"]] += 1

    answers = {
        "seed": SEED,
        "raw_row_count": len(rows),
        "valid_orders": len(kept),
        "dropped_total": len(rows) - len(kept),
        "dropped_by_reason": dict(sorted(dropped.items())),
        "total_revenue": total,
        "avg_order_value": round(total / len(kept), 2),
        "revenue_by_region": agg(lambda t: t["region"]),
        "revenue_by_category": agg(lambda t: t["category"]),
        "revenue_by_month": by_month,
        "top5_products": [{"product": p, "revenue": round(v, 2)} for p, v in prod_rev[:5]],
        "unique_customers": len({t["customer_id"] for t in kept}),
        "kept_order_ids": sorted(t["order_id"] for t in kept),
    }
    (GT / "answers.json").write_text(json.dumps(answers, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
    print(f"[ok] 原始行数 {len(rows)}，有效 {len(kept)}，剔除 {len(rows)-len(kept)}")
    print("     " + json.dumps(dict(dropped), ensure_ascii=False))
    print(f"[ok] 答案 -> {GT/'answers.json'}")


def _sum(items, key):
    out = defaultdict(float)
    for t in items:
        out[key(t) if callable(key) else t[key]] += t["revenue"]
    return out


if __name__ == "__main__":
    main()
