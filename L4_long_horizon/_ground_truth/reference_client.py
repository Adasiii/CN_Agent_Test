"""L4 参考解（仅标准库）——验证故障编排可通过、答案自洽、score.py 正确。
不要交给被测 Agent。用法：python reference_client.py --ws <输出目录> [--port 8741]
"""
import argparse
import json
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime
from pathlib import Path

DATES = ["2026-07-27", "2026-07-28", "2026-07-29", "2026-07-30"]


class Client:
    def __init__(self, ws: Path, port: int):
        self.ws = ws
        self.base = f"http://127.0.0.1:{port}/api/v1"
        (ws / "raw").mkdir(parents=True, exist_ok=True)
        (ws / "reports").mkdir(exist_ok=True)
        (ws / "outbox").mkdir(exist_ok=True)
        self.state = json.loads((ws / "state.json").read_text(encoding="utf-8")) \
            if (ws / "state.json").exists() else {"round": 0, "days": {}, "pending_todo": []}

    # ---------- 基础 ----------
    def get(self, d, page, cursor=None):
        url = f"{self.base}/records?date={d}&page={page}" + (f"&cursor={cursor}" if cursor else "")
        req = urllib.request.Request(url)
        try:
            r = urllib.request.urlopen(req, timeout=20)
            return r.status, r.read().decode(), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(), dict(e.headers)

    def exc(self, **kw):
        kw["ts"] = datetime.now().isoformat(timespec="seconds")
        with (self.ws / "exceptions.log").open("a", encoding="utf-8") as f:
            f.write(json.dumps(kw, ensure_ascii=False) + "\n")

    def fetch_page(self, d, page, cursor=None, max_retries=4):
        delay = 1
        for attempt in range(1, max_retries + 1):
            code, body, hdr = self.get(d, page, cursor)
            if code == 200:
                try:
                    data = json.loads(body)
                except json.JSONDecodeError:
                    self.exc(date=d, page=page, http_status=200, attempts=attempt,
                             action="残缺 JSON，重试", resolved=False)
                    time.sleep(0.2)
                    continue
                if attempt > 1:
                    self.exc(date=d, page=page, http_status=code, attempts=attempt,
                             action="重试后成功", resolved=True)
                return data
            if code == 400:
                self.exc(date=d, page=page, http_status=400, attempts=attempt,
                         action=f"参数错误，按 hint 修正: {body[:80]}", resolved=False)
                return None
            self.exc(date=d, page=page, http_status=code, attempts=attempt,
                     action=f"退避 {delay}s 后重试", resolved=False)
            time.sleep(min(delay, 1) * 0.2)   # 参考解压缩等待时间
            delay *= 2
        self.exc(date=d, page=page, http_status=code, attempts=max_retries,
                 action="重试耗尽，记为缺口，跳过", resolved=False)
        return None

    # ---------- 抓取一天 ----------
    def fetch_day(self, d):
        day = self.state["days"].setdefault(d, {"pages_done": [], "gaps": [], "records": 0})
        seen = self.existing_ids(d)
        cursor = None
        for page in (1, 2, 3):
            if page in day["pages_done"]:
                continue
            data = self.fetch_page(d, page, cursor)
            if data is None and page == 2 and d == DATES[3]:
                data = self.fetch_page(d, page, cursor)          # 带上 cursor 重来
            if data is None:
                if page not in day["gaps"]:
                    day["gaps"].append(page)
                continue
            cursor = data.get("next_cursor")
            n = self.append_records(d, data["records"], seen)
            day["records"] += n
            day["pages_done"].append(page)
            if page in day["gaps"]:
                day["gaps"].remove(page)
                self.exc(date=d, page=page, http_status=200, attempts=1,
                         action="缺口回填成功", resolved=True)
        self.save()

    def existing_ids(self, d):
        f = self.ws / "raw" / f"records_{d}.jsonl"
        ids = set()
        if f.exists():
            for line in f.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    ids.add(json.loads(line)["record_id"])
        return ids

    def append_records(self, d, records, seen):
        n = 0
        with (self.ws / "raw" / f"records_{d}.jsonl").open("a", encoding="utf-8") as f:
            for r in records:
                if r["record_id"] in seen:
                    continue
                seen.add(r["record_id"])
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
                n += 1
        return n

    # ---------- 汇总 ----------
    def day_stats(self, d):
        agg = {"valid_records": 0, "anomalies": 0, "amount": 0.0, "qty": 0}
        stores = defaultdict(float)
        f = self.ws / "raw" / f"records_{d}.jsonl"
        if f.exists():
            for line in f.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                r = json.loads(line)
                if r.get("qty") is None:
                    agg["anomalies"] += 1
                    continue
                amt = float(str(r["amount"]).replace(",", ""))
                agg["valid_records"] += 1
                agg["amount"] += amt
                agg["qty"] += r["qty"]
                stores[r["store_id"]] += amt
        agg["amount"] = round(agg["amount"], 2)
        top = sorted(stores.items(), key=lambda kv: -kv[1])[:3]
        return agg, top

    def write_totals_and_reports(self):
        per_day, overall = {}, {"valid_records": 0, "anomalies": 0, "amount": 0.0, "qty": 0}
        for d in DATES:
            if not (self.ws / "raw" / f"records_{d}.jsonl").exists():
                continue
            agg, top = self.day_stats(d)
            per_day[d] = agg
            for k in overall:
                overall[k] += agg[k]
            gaps = self.state["days"].get(d, {}).get("gaps", [])
            (self.ws / "reports" / f"daily_{d}.md").write_text(
                f"# {d} 日报\n\n- 有效记录：{agg['valid_records']}\n- 异常记录：{agg['anomalies']}\n"
                f"- 金额合计：{agg['amount']:,.2f}\n- 数量合计：{agg['qty']}\n"
                f"- Top3 门店：{', '.join(f'{s}({v:,.0f})' for s, v in top)}\n"
                f"- 缺口：{gaps if gaps else '无'}\n", encoding="utf-8")
        overall["amount"] = round(overall["amount"], 2)
        (self.ws / "totals.json").write_text(
            json.dumps({"per_day": per_day, "overall": overall}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        return per_day, overall

    def save(self):
        self.state["updated_at"] = datetime.now().isoformat(timespec="seconds")
        (self.ws / "state.json").write_text(
            json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")

    def notes(self, text):
        (self.ws / "NOTES.md").write_text(text, encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ws", default=str(Path(__file__).resolve().parents[1] / "workspace"))
    ap.add_argument("--port", type=int, default=8741)
    a = ap.parse_args()
    ws = Path(a.ws); ws.mkdir(parents=True, exist_ok=True)
    c = Client(ws, a.port)

    # 第 1 轮
    c.state["round"] = 1
    c.fetch_day(DATES[0])
    c.write_totals_and_reports()
    c.notes(f"# 交接备忘\n\n- 已完成 {DATES[0]} 全部 3 页。\n- 下一轮：抓取 {DATES[1]}。\n")

    # 第 2 轮
    c.state["round"] = 2
    c.fetch_day(DATES[1])
    c.write_totals_and_reports()
    c.notes(f"# 交接备忘\n\n- 已完成 {DATES[0]}；{DATES[1]} 的 page 3 反复 500，"
            f"重试耗尽，记为**未解决缺口**。\n- 下一轮第一件事：重试 {DATES[1]} page 3 回填，"
            f"然后抓 {DATES[2]}。\n")

    # 第 3 轮：先抓 D3（触发对方修复），再回填 D2 缺口
    c.state["round"] = 3
    c.fetch_day(DATES[2])
    c.fetch_day(DATES[1])
    c.write_totals_and_reports()
    c.notes(f"# 交接备忘\n\n- {DATES[1]} page 3 缺口已回填并更新日报。\n"
            f"- 下一轮：抓 {DATES[3]}（page2 需要 cursor），做总汇总与定时配置。\n")

    # 第 4 轮
    c.state["round"] = 4
    c.fetch_day(DATES[3])
    per_day, overall = c.write_totals_and_reports()
    (ws / "schedule.json").write_text(json.dumps({
        "cron": "0 9 * * *", "timezone": "Asia/Shanghai",
        "command": "python pipeline.py --date {{today}}",
        "deliverable": "outbox/digest_{{date}}.md",
        "retry_policy": {"max_retries": 4, "backoff": "exponential:1s,2s,4s,8s"},
        "on_failure": "记录 exceptions.log 并在次日交付中标注缺口，连续 2 天失败则告警",
        "created_via": "配置文件（本环境无真实调度器）",
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    (ws / "outbox" / f"digest_{DATES[3]}.md").write_text(
        f"# {DATES[3]} 日报摘要\n\n当日有效记录 {per_day[DATES[3]]['valid_records']} 条，"
        f"金额 {per_day[DATES[3]]['amount']:,.2f} 元。四日累计有效记录 "
        f"{overall['valid_records']} 条，金额 {overall['amount']:,.2f} 元，"
        f"数量 {overall['qty']}。数据完整性：全部 4 天 12 页均已抓全"
        f"（{DATES[1]} page3 曾缺失，已于次轮回填）。\n", encoding="utf-8")
    lines = "\n".join(
        f"| {d} | {v['valid_records']} | {v['anomalies']} | {v['amount']:,.2f} | {v['qty']} |"
        for d, v in per_day.items())
    (ws / "summary_final.md").write_text(
        f"# 四日汇总（参考解）\n\n| 日期 | 有效记录 | 异常 | 金额 | 数量 |\n|---|---|---|---|---|\n"
        f"{lines}\n| **合计** | **{overall['valid_records']}** | **{overall['anomalies']}** | "
        f"**{overall['amount']:,.2f}** | **{overall['qty']}** |\n\n"
        f"## 异常处理\n- {DATES[0]} page2：500×2，重试第 3 次成功。\n"
        f"- {DATES[0]} page3：429，按 Retry-After 退避后成功。\n"
        f"- {DATES[1]} page3：持续 500，第 2 轮记为缺口，第 3 轮回填成功。\n"
        f"- {DATES[2]} page1：残缺 JSON，重试后成功。\n"
        f"- {DATES[3]} page2：400 缺少 cursor，改用 page1 的 next_cursor 后成功。\n\n"
        f"## 数据完整性\n4 天 × 3 页全部抓齐，去重后无重复计数。\n", encoding="utf-8")
    c.save()
    print(json.dumps({"per_day": per_day, "overall": overall}, ensure_ascii=False))


if __name__ == "__main__":
    main()
