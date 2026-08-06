"""L3b 参考解（仅标准库）——用于验证题目可解、清洗规则自洽、score.py 正确。
不要交给被测 Agent。用法：python reference_solution.py --out <目录>
"""
import argparse
import csv
import json
import re
from collections import defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CUTOFF = date(2026, 7, 31)
MISSING = {"", "na", "n/a", "null", "-", "未知", "none"}
REGION = {"华东": "华东", "华东区": "华东", "east": "华东",
          "华北": "华北", "华北区": "华北", "north": "华北",
          "华南": "华南", "华南区": "华南", "south": "华南",
          "西部": "西部", "西区": "西部", "west": "西部"}


def parse_date(s):
    s = s.strip()
    for pat, order in ((r"^(\d{4})-(\d{2})-(\d{2})$", "ymd"),
                       (r"^(\d{4})/(\d{1,2})/(\d{1,2})$", "ymd"),
                       (r"^(\d{1,2})-(\d{1,2})-(\d{4})$", "dmy"),
                       (r"^(\d{4})年(\d{1,2})月(\d{1,2})日$", "ymd")):
        m = re.match(pat, s)
        if m:
            a, b, c = (int(x) for x in m.groups())
            y, mo, d = (a, b, c) if order == "ymd" else (c, b, a)
            try:
                return date(y, mo, d)
            except ValueError:
                return None
    return None


def num(s):
    s = str(s).replace("¥", "").replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "_ref_output"))
    a = ap.parse_args()
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)

    with (ROOT / "workspace" / "orders_raw.csv").open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    seen, kept, dropped = set(), [], []
    for r in rows:
        sig = tuple((r.get(k) or "") for k in r)
        if sig in seen:
            dropped.append((r["order_id"], "duplicate")); continue
        seen.add(sig)

        q_raw = (r["quantity"] or "").strip()
        p_raw = (r["unit_price"] or "").strip()
        if q_raw.lower() in MISSING or p_raw.lower() in MISSING:
            dropped.append((r["order_id"], "missing_value")); continue
        d = parse_date(r["order_date"])
        if d is None or d > CUTOFF:
            dropped.append((r["order_id"], "invalid_date")); continue
        q, p = num(q_raw), num(p_raw)
        disc_raw = (r["discount_rate"] or "").strip()
        disc = 0.0 if disc_raw.lower() in MISSING else num(disc_raw)
        if q is None or q <= 0 or q > 1000:
            dropped.append((r["order_id"], "outlier_quantity")); continue
        if p is None or p <= 0:
            dropped.append((r["order_id"], "outlier_price")); continue
        if disc is None or not (0 <= disc <= 0.9):
            dropped.append((r["order_id"], "outlier_discount")); continue

        reg = (r["region"] or "").strip()
        reg = REGION.get(reg.lower(), REGION.get(reg, "未知" if reg.lower() in MISSING else reg))
        kept.append({"order_id": r["order_id"], "order_date": d.isoformat(), "region": reg,
                     "channel": r["channel"], "category": r["category"], "product": r["product"],
                     "customer_id": r["customer_id"], "quantity": int(q), "unit_price": p,
                     "discount_rate": disc, "revenue": round(q * p * (1 - disc), 2)})

    cols = ["order_id", "order_date", "region", "channel", "category", "product",
            "customer_id", "quantity", "unit_price", "discount_rate", "revenue"]
    with (out / "cleaned.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, cols); w.writeheader(); w.writerows(kept)
    with (out / "dropped.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f); w.writerow(["order_id", "drop_reason"]); w.writerows(dropped)

    agg = lambda key: dict(sorted(_sum(kept, key).items(), key=lambda kv: -kv[1]))
    total = round(sum(k["revenue"] for k in kept), 2)
    by_reason = defaultdict(int)
    for _, why in dropped:
        by_reason[why] += 1
    prod = sorted(_sum(kept, "product").items(), key=lambda kv: -kv[1])[:5]
    metrics = {
        "valid_orders": len(kept), "dropped_total": len(dropped),
        "dropped_by_reason": dict(sorted(by_reason.items())),
        "total_revenue": total, "avg_order_value": round(total / len(kept), 2),
        "unique_customers": len({k["customer_id"] for k in kept}),
        "revenue_by_region": agg("region"), "revenue_by_category": agg("category"),
        "revenue_by_month": dict(sorted(_sum(kept, lambda k: k["order_date"][:7]).items())),
        "top5_products": [{"product": p, "revenue": round(v, 2)} for p, v in prod],
    }
    (out / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2),
                                      encoding="utf-8")

    bar(out / "chart_region.svg", metrics["revenue_by_region"], "分大区营收（元）")
    bar(out / "chart_category.svg", metrics["revenue_by_category"], "分品类营收（元）")
    bar(out / "chart_monthly.svg", metrics["revenue_by_month"], "分月营收（元）")
    (out / "report.md").write_text(
        f"# 订单数据清洗与分析（参考解）\n\n原始 {len(rows)} 行，有效 {len(kept)} 单，"
        f"剔除 {len(dropped)} 行。总营收 {total:,.2f} 元，平均订单金额 {metrics['avg_order_value']} 元。\n\n"
        "![](chart_monthly.svg)\n![](chart_region.svg)\n![](chart_category.svg)\n\n"
        "## 口径与局限\n按题面清洗规则执行，缺失值直接剔除未插补，结论受该口径影响。\n",
        encoding="utf-8")
    print(json.dumps(metrics, ensure_ascii=False)[:300])
    print(f"[ok] 参考输出 -> {out}")


def _sum(items, key):
    o = defaultdict(float)
    for t in items:
        o[key(t) if callable(key) else t[key]] += t["revenue"]
    return {k: round(v, 2) for k, v in o.items()}


def bar(path, data, title):
    w, h, pad = 760, 380, 60
    mx = max(data.values()) or 1
    bw = (w - 2 * pad) / max(len(data), 1)
    parts = [f'<rect width="{w}" height="{h}" fill="#fff"/>',
             f'<text x="{w/2}" y="28" font-size="18" text-anchor="middle" '
             f'font-family="Microsoft YaHei,SimHei,sans-serif">{title}</text>',
             f'<line x1="{pad}" y1="{h-pad}" x2="{w-pad}" y2="{h-pad}" stroke="#333"/>',
             f'<line x1="{pad}" y1="{pad-10}" x2="{pad}" y2="{h-pad}" stroke="#333"/>']
    for i, (k, v) in enumerate(data.items()):
        bh = (v / mx) * (h - 2 * pad - 20)
        x = pad + i * bw + bw * 0.15
        parts.append(f'<rect x="{x:.1f}" y="{h-pad-bh:.1f}" width="{bw*0.7:.1f}" '
                     f'height="{bh:.1f}" fill="#3b7dd8"/>')
        parts.append(f'<text x="{x+bw*0.35:.1f}" y="{h-pad+16}" font-size="11" '
                     f'text-anchor="middle" font-family="Microsoft YaHei,SimHei,sans-serif">{k}</text>')
        parts.append(f'<text x="{x+bw*0.35:.1f}" y="{h-pad-bh-4:.1f}" font-size="10" '
                     f'text-anchor="middle">{v:,.0f}</text>')
    Path(path).write_text(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
        f'viewBox="0 0 {w} {h}">{"".join(parts)}</svg>', encoding="utf-8")


if __name__ == "__main__":
    main()
