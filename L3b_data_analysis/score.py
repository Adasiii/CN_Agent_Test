"""L3b 自动评分：对比 ground truth 的 answers.json。

用法：
    python score.py                          # 评分 ./workspace，GT 用 ./_ground_truth
    python score.py --ws <被测工作目录>
    python score.py --gt <answers.json>      # _ground_truth 搬到别处时用
    python score.py --rerun                  # 额外：在干净临时目录重跑 analysis.py 检验可复现性
工作目录和 GT 都可以挪到任意位置，两者不必相邻。
"""
import argparse
import csv
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_GT = ROOT / "_ground_truth" / "answers.json"
GT = {}          # 在 main() 里按 --gt 加载
TOL = 0.005      # 相对误差 0.5%


def close(a, b):
    try:
        a, b = float(a), float(b)
    except (TypeError, ValueError):
        return False
    return abs(a - b) <= max(TOL * max(abs(b), 1.0), 0.02)


def cmp_dict(got, want):
    if not isinstance(got, dict):
        return 0.0, ["类型错误，期望 object"]
    bad = []
    hit = 0
    for k, v in want.items():
        g = got.get(k)
        if close(g, v):
            hit += 1
        else:
            bad.append(f"{k}: 期望 {v} 实际 {g}")
    extra = [k for k in got if k not in want]
    if extra:
        bad.append(f"多余的键: {extra}")
    return hit / len(want), bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ws", default=str(ROOT / "workspace"))
    ap.add_argument("--gt", default=str(DEFAULT_GT),
                    help="ground truth 的 answers.json 路径；传目录也可以")
    ap.add_argument("--rerun", action="store_true")
    ap.add_argument("--json")
    a = ap.parse_args()
    gt_file = Path(a.gt).resolve()
    if gt_file.is_dir():
        gt_file = gt_file / "answers.json"
    if not gt_file.exists():
        raise SystemExit(f"找不到 ground truth: {gt_file}\n用 --gt 指定 answers.json 的位置")
    global GT
    GT = json.loads(gt_file.read_text(encoding="utf-8"))
    ws = Path(a.ws).resolve()
    res = {"metrics": {}, "errors": []}

    # ---------- 1. metrics.json ----------
    mp = ws / "metrics.json"
    scalar_score = dict_score = 0.0
    if not mp.exists():
        res["errors"].append("缺少 metrics.json")
        m = {}
    else:
        try:
            m = json.loads(mp.read_text(encoding="utf-8-sig"))
        except Exception as e:
            res["errors"].append(f"metrics.json 解析失败: {e}")
            m = {}
        scalars = ["valid_orders", "dropped_total", "total_revenue",
                   "avg_order_value", "unique_customers"]
        det = {}
        for k in scalars:
            det[k] = {"expected": GT[k], "got": m.get(k), "ok": close(m.get(k), GT[k])}
        scalar_score = sum(d["ok"] for d in det.values()) / len(scalars)
        res["metrics"]["scalars"] = det

        parts = {}
        for key in ["dropped_by_reason", "revenue_by_region",
                    "revenue_by_category", "revenue_by_month"]:
            s, bad = cmp_dict(m.get(key, {}), GT[key])
            parts[key] = {"accuracy": round(s, 3), "problems": bad[:6]}
        top_got = [(x.get("product"), x.get("revenue")) for x in m.get("top5_products", [])
                   if isinstance(x, dict)]
        top_want = [(x["product"], x["revenue"]) for x in GT["top5_products"]]
        top_hit = sum(1 for i, (p, v) in enumerate(top_want)
                      if i < len(top_got) and top_got[i][0] == p and close(top_got[i][1], v))
        parts["top5_products"] = {"accuracy": round(top_hit / 5, 3),
                                  "expected": top_want, "got": top_got}
        dict_score = sum(p["accuracy"] for p in parts.values()) / len(parts)
        res["metrics"]["breakdown"] = parts

    # ---------- 2. cleaned.csv / dropped.csv ----------
    def read_ids(name, col="order_id"):
        p = ws / name
        if not p.exists():
            res["errors"].append(f"缺少 {name}")
            return None
        with p.open(encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))

    clean_f1 = 0.0
    rows = read_ids("cleaned.csv")
    if rows is not None:
        got = {(r.get("order_id") or "").strip() for r in rows}
        want = set(GT["kept_order_ids"])
        tp = len(got & want)
        prec = tp / len(got) if got else 0
        rec = tp / len(want)
        clean_f1 = 2 * prec * rec / (prec + rec) if tp else 0.0
        res["metrics"]["cleaned_csv"] = {
            "rows": len(rows), "expected_rows": len(want),
            "precision": round(prec, 4), "recall": round(rec, 4), "f1": round(clean_f1, 4),
            "missing_examples": sorted(want - got)[:5], "extra_examples": sorted(got - want)[:5],
            "has_revenue_col": bool(rows) and "revenue" in rows[0],
        }

    drop_acc = 0.0
    drows = read_ids("dropped.csv")
    if drows is not None:
        cnt = {}
        for r in drows:
            reason = (r.get("drop_reason") or "").strip()
            cnt[reason] = cnt.get(reason, 0) + 1
        s, bad = cmp_dict(cnt, GT["dropped_by_reason"])
        drop_acc = s
        res["metrics"]["dropped_csv"] = {"rows": len(drows), "by_reason": cnt,
                                         "accuracy": round(s, 3), "problems": bad[:6]}

    # ---------- 3. 图表与报告 ----------
    charts = [p.name for p in ws.glob("chart_*.*")
              if p.suffix.lower() in (".png", ".svg", ".jpg", ".jpeg")]
    res["metrics"]["charts"] = charts
    chart_ok = len(charts) >= 3
    tiny = [c for c in charts
            if (ws / c).stat().st_size < (800 if c.lower().endswith(".svg") else 3000)]
    if tiny:
        res["errors"].append(f"以下图表文件过小，可能是空图: {tiny}")

    rep = ws / "report.md"
    rep_txt = rep.read_text(encoding="utf-8") if rep.exists() else ""
    embedded = len(re.findall(r"!\[[^\]]*\]\([^)]+\)", rep_txt))
    # 报告—指标一致性：报告里出现的整数金额是否与关键指标吻合
    consistency = None
    if rep_txt and m:
        nums = {n.replace(",", "") for n in re.findall(r"\d[\d,]{2,}(?:\.\d+)?", rep_txt)}
        key_vals = [GT["total_revenue"], GT["valid_orders"], GT["avg_order_value"]]
        consistency = sum(any(close(n, kv) for n in nums) for kv in key_vals) / len(key_vals)
    res["metrics"]["report"] = {
        "exists": rep.exists(), "chars": len(rep_txt), "embedded_charts": embedded,
        "mentions_limitations": any(k in rep_txt for k in ("局限", "口径", "假设")),
        "key_number_consistency": consistency,
    }

    # ---------- 4. 可复现性 ----------
    script = next((ws / n for n in ("analysis.py", "analysis.ipynb") if (ws / n).exists()), None)
    repro = {"script_present": script.name if script else None, "rerun": None}
    if a.rerun and script and script.suffix == ".py":
        tmp = Path(tempfile.mkdtemp(prefix="l3b_repro_"))
        for n in ("orders_raw.csv", "orders_raw.xlsx", "data_dictionary.md", script.name):
            if (ws / n).exists():
                shutil.copy2(ws / n, tmp / n)
        r = subprocess.run([sys.executable, script.name], cwd=tmp,
                           capture_output=True, text=True, timeout=600)
        produced = sorted(p.name for p in tmp.iterdir())
        same_metrics = None
        if (tmp / "metrics.json").exists() and m:
            try:
                same_metrics = json.loads((tmp / "metrics.json").read_text(encoding="utf-8-sig")) == m
            except Exception:
                same_metrics = False
        repro["rerun"] = {"returncode": r.returncode, "stderr_tail": r.stderr[-500:],
                          "produced": produced, "metrics_identical": same_metrics,
                          "tmpdir": str(tmp)}
    res["metrics"]["reproducibility"] = repro

    # ---------- 总分 ----------
    score = (
        scalar_score * 20
        + dict_score * 25
        + clean_f1 * 20
        + drop_acc * 10
        + (10 if chart_ok else len(charts) * 3)
        + (5 if embedded >= 3 else 0)
        + ((consistency or 0) * 5)
        + (5 if repro["script_present"] else 0)
    )
    # --rerun 会额外考核可复现性，满分从 100 变成 105，因此统一归一化到 100 分制，
    # 否则跑了和没跑 --rerun 的结果不能放在同一张表里比。
    max_score = 100
    if a.rerun and repro["rerun"]:
        max_score = 105
        score += 5 if repro["rerun"]["returncode"] == 0 and repro["rerun"]["metrics_identical"] else 0
        res["score_breakdown"] = ("标量20+分组25+cleaned20+dropped10+图表10+嵌图5+报告一致5"
                                  "+脚本5+复现5 = 105，已归一化到 100")
    else:
        res["score_breakdown"] = ("标量20+分组25+cleaned20+dropped10+图表10+嵌图5+报告一致5"
                                  "+脚本5 = 100（未跑 --rerun，可复现性未考核）")
        res["warning"] = "未使用 --rerun，可复现性没有被验证；与跑过 --rerun 的结果不完全可比"
    res["score_raw"] = round(score, 2)
    res["max_score"] = max_score
    res["score"] = round(score / max_score * 100, 2)

    print(json.dumps(res, ensure_ascii=False, indent=2))
    print(f"\n=== L3b 得分: {res['score']} / 100 ===")
    if a.json:
        Path(a.json).write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
