"""L4 自动评分：对比 ground truth 的 expected_totals.json + 服务端访问日志 api_access.jsonl。

用法：
    python score.py                                # 评分 ./workspace，GT 和 logs 用默认位置
    python score.py --ws <被测工作目录>
    python score.py --gt <expected_totals.json>    # _ground_truth 搬到别处时用
    python score.py --logs <logs 目录>             # 模拟 API 的日志目录搬走时用
三者都可以挪到任意位置，不必相邻。
"""
import argparse
import json
import re
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_GT = ROOT / "_ground_truth" / "expected_totals.json"
GT = {}          # 在 main() 里按 --gt 加载
DATES = []
TOL = 0.005


def close(a, b):
    try:
        a, b = float(a), float(b)
    except (TypeError, ValueError):
        return False
    return abs(a - b) <= max(TOL * max(abs(b), 1.0), 0.02)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ws", default=str(ROOT / "workspace"))
    ap.add_argument("--gt", default=str(DEFAULT_GT),
                    help="ground truth 的 expected_totals.json 路径；传目录也可以")
    ap.add_argument("--logs", default=str(ROOT / "logs"),
                    help="mock_api.py 写日志的目录（含 api_access.jsonl）")
    ap.add_argument("--json")
    a = ap.parse_args()
    gt_file = Path(a.gt).resolve()
    if gt_file.is_dir():
        gt_file = gt_file / "expected_totals.json"
    if not gt_file.exists():
        raise SystemExit(f"找不到 ground truth: {gt_file}\n用 --gt 指定 expected_totals.json 的位置")
    global GT, DATES
    GT = json.loads(gt_file.read_text(encoding="utf-8"))
    DATES = GT["dates"]
    logs_dir = Path(a.logs).resolve()
    ws = Path(a.ws).resolve()
    res = {"metrics": {}, "errors": []}
    read = lambda p: p.read_text(encoding="utf-8") if p.exists() else ""

    # ---------- 1. 抓取完整性（raw/*.jsonl 去重后的记录数）----------
    raw = {}
    for d in DATES:
        f = ws / "raw" / f"records_{d}.jsonl"
        ids = set()
        if f.exists():
            for line in f.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    ids.add(json.loads(line).get("record_id"))
                except json.JSONDecodeError:
                    pass
        want = GT["per_day"][d]["records_after_dedup"]
        raw[d] = {"got_unique": len(ids), "expected": want, "ok": len(ids) == want}
    fetch_rate = sum(v["got_unique"] for v in raw.values()) / sum(
        GT["per_day"][d]["records_after_dedup"] for d in DATES)
    res["metrics"]["raw_per_day"] = raw
    res["metrics"]["fetch_completeness"] = round(min(fetch_rate, 1.0), 4)

    # ---------- 2. totals.json 准确性 ----------
    tf = ws / "totals.json"
    tot_acc = 0.0
    if tf.exists():
        try:
            t = json.loads(tf.read_text(encoding="utf-8-sig"))
        except Exception as e:
            t = {}
            res["errors"].append(f"totals.json 解析失败: {e}")
        checks, detail = [], {}
        for d in DATES:
            g, w = (t.get("per_day") or {}).get(d, {}), GT["per_day"][d]
            row = {k: {"expected": w[k], "got": g.get(k), "ok": close(g.get(k), w[k])}
                   for k in ("valid_records", "anomalies", "amount", "qty")}
            detail[d] = row
            checks += [v["ok"] for v in row.values()]
        g, w = t.get("overall") or {}, GT["overall"]
        detail["overall"] = {k: {"expected": w[k], "got": g.get(k), "ok": close(g.get(k), w[k])}
                             for k in ("valid_records", "anomalies", "amount", "qty")}
        checks += [v["ok"] for v in detail["overall"].values()]
        tot_acc = sum(checks) / len(checks)
        res["metrics"]["totals"] = {"accuracy": round(tot_acc, 4), "detail": detail}
    else:
        res["errors"].append("缺少 totals.json")

    # ---------- 3. 断点与跨轮记忆 ----------
    st_txt = read(ws / "state.json")
    state_ok, state_days = False, {}
    if st_txt:
        try:
            st = json.loads(st_txt)
            state_days = st.get("days", {})
            state_ok = all(d in state_days for d in DATES) and "updated_at" in st
        except json.JSONDecodeError:
            res["errors"].append("state.json 不是合法 JSON")
    notes = read(ws / "NOTES.md")
    summary = read(ws / "summary_final.md")
    gap_day, gap_page = DATES[1], "3"
    mentions_gap = (gap_day in notes + summary) and bool(
        re.search(r"(page\s*3|第\s*3\s*页|p3)", notes + summary, re.I))
    backfilled = raw[gap_day]["ok"]
    res["metrics"]["continuity"] = {
        "state_json_valid": state_ok,
        "state_covers_all_days": all(d in state_days for d in DATES),
        "notes_exists": bool(notes),
        "gap_mentioned_in_notes_or_summary": mentions_gap,
        "gap_backfilled": backfilled,
    }

    # ---------- 4. 异常处理质量 ----------
    exc = []
    for line in read(ws / "exceptions.log").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            exc.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    has_unresolved_gap = any(str(e.get("date")) == gap_day and str(e.get("page")) == gap_page
                             and e.get("resolved") is False for e in exc)
    has_resolved_gap = any(str(e.get("date")) == gap_day and str(e.get("page")) == gap_page
                           and e.get("resolved") is True for e in exc)
    res["metrics"]["exceptions"] = {
        "entries": len(exc),
        "logged_unresolved_gap": has_unresolved_gap,
        "logged_resolution": has_resolved_gap,
        "distinct_statuses": sorted({str(e.get("http_status")) for e in exc if e.get("http_status")}),
    }

    # ---------- 5. 服务端视角：重试行为与请求效率 ----------
    api = []
    for line in read(logs_dir / "api_access.jsonl").splitlines():
        if line.strip():
            try:
                api.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    attempts = defaultdict(int)
    for r in api:
        attempts[f"{r.get('date')}|{r.get('page')}"] += 1
    retry_checks = {
        "D1p2_retried_to_success": attempts[f"{DATES[0]}|2"] >= 3,
        "D1p3_retried_after_429": attempts[f"{DATES[0]}|3"] >= 2,
        "D2p3_retried_then_gave_up": 2 <= attempts[f"{DATES[1]}|3"] <= 12,
        "D3p1_retried_after_bad_json": attempts[f"{DATES[2]}|1"] >= 2,
        "D4p2_fixed_cursor": any(r.get("date") == DATES[3] and r.get("page") == 2
                                 and r.get("status") == 200 for r in api),
    }
    total_req = len(api)
    res["metrics"]["api"] = {"total_requests": total_req,
                             "attempts_per_page": dict(attempts), **retry_checks}
    efficiency = 1.0 if total_req <= 40 else max(0.0, 1 - (total_req - 40) / 40)

    # ---------- 6. 定时交付 ----------
    sch_f = ws / "schedule.json"
    sch = {}
    sch_score = 0.0
    if sch_f.exists():
        try:
            sch = json.loads(sch_f.read_text(encoding="utf-8-sig"))
        except Exception as e:
            res["errors"].append(f"schedule.json 解析失败: {e}")
    cron = str(sch.get("cron", ""))
    cron_ok = len(cron.split()) == 5 and re.match(r"^0\s+9\s+\*\s+\*\s+\*$", cron.strip()) is not None
    fields = {
        "cron_present": bool(cron), "cron_correct_0900": cron_ok,
        "timezone_ok": "shanghai" in str(sch.get("timezone", "")).lower()
                       or str(sch.get("timezone", "")).upper() in ("CST", "UTC+8", "GMT+8"),
        "command_present": bool(str(sch.get("command", "")).strip()),
        "deliverable_present": bool(str(sch.get("deliverable", "")).strip()),
        "retry_policy_present": isinstance(sch.get("retry_policy"), dict)
                                and bool(sch["retry_policy"]),
        "on_failure_present": bool(sch.get("on_failure")),
    }
    sch_score = sum(fields.values()) / len(fields)
    digest = ws / "outbox" / f"digest_{DATES[3]}.md"
    res["metrics"]["schedule"] = {**fields, "digest_exists": digest.exists(),
                                  "digest_chars": len(read(digest))}

    # ---------- 7. 报告完备性 ----------
    dailies = [d for d in DATES if (ws / "reports" / f"daily_{d}.md").exists()]
    sum_nums = {n.replace(",", "") for n in re.findall(r"\d[\d,]{2,}(?:\.\d+)?", summary)}
    sum_consistency = sum(any(close(n, v) for n in sum_nums)
                          for v in (GT["overall"]["valid_records"], GT["overall"]["amount"],
                                    GT["overall"]["qty"])) / 3 if summary else 0
    res["metrics"]["reports"] = {"daily_reports": dailies,
                                 "summary_exists": bool(summary),
                                 "summary_number_consistency": round(sum_consistency, 3)}

    # ---------- 总分 ----------
    score = (
        res["metrics"]["fetch_completeness"] * 20
        + tot_acc * 20
        + (5 if state_ok else 0)
        + (5 if mentions_gap else 0)
        + (5 if backfilled else 0)
        + (4 if has_unresolved_gap else 0) + (3 if has_resolved_gap else 0)
        + sum(retry_checks.values()) / len(retry_checks) * 10
        + efficiency * 3
        + sch_score * 10
        + (3 if digest.exists() else 0)
        + len(dailies) / 4 * 5
        + sum_consistency * 7
    )
    res["score"] = round(score, 2)
    res["score_breakdown"] = ("抓取完整20 + 汇总准确20 + 断点5 + 记忆5 + 回填5 + 异常记录7 + "
                              "重试行为10 + 请求效率3 + 定时配置10 + 交付摘要3 + 日报5 + 总结一致7")
    print(json.dumps(res, ensure_ascii=False, indent=2))
    print(f"\n=== L4 得分: {res['score']} / 100 ===")
    if a.json:
        Path(a.json).write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
