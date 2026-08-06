"""L4 模拟业务 API：分页数据 + 确定性故障注入 + 访问日志。纯标准库。

启动：python mock_api.py [--port 8741] [--reset]

接口：
  GET /api/v1/health
  GET /api/v1/records?date=YYYY-MM-DD&page=N[&cursor=...]
      -> {"date","page","total_pages","next_cursor","records":[...]}
  GET /api/v1/dates            可用日期列表

故障编排（对同一 key 的第几次请求决定结果，状态持久化到 logs/server_state.json，
服务重启不会重置，便于测试「Agent 中断后恢复」）：
  D1/page2  前 2 次 500，第 3 次起 200
  D1/page3  第 1 次 429（Retry-After: 2），之后 200
  D2/page3  持续 500，直到有人请求过 D3 的任意页（模拟「服务方次日修复」）→ 之后 200
  D3/page1  第 1 次返回残缺 JSON（200 但 body 截断），之后 200
  D4/page2  必须带上 page1 返回的 next_cursor，否则 400
"""
import argparse
import hashlib
import json
import random
import re
import shutil
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
LOGS = ROOT / "logs"
GT = ROOT / "_ground_truth"
DATES = ["2026-07-27", "2026-07-28", "2026-07-29", "2026-07-30"]
PAGES = 3
PER_PAGE = 40
STORES = ["S01", "S02", "S03", "S04", "S05"]
SKUS = ["SKU-A1", "SKU-B2", "SKU-C3", "SKU-D4", "SKU-E5", "SKU-F6"]
STATE = {"counts": {}, "d3_touched": False}


def save_state():
    LOGS.mkdir(exist_ok=True)
    (LOGS / "server_state.json").write_text(json.dumps(STATE), encoding="utf-8")


def load_state():
    p = LOGS / "server_state.json"
    if p.exists():
        STATE.update(json.loads(p.read_text(encoding="utf-8")))


def log(rec):
    LOGS.mkdir(exist_ok=True)
    rec["ts"] = datetime.now().isoformat(timespec="seconds")
    with (LOGS / "api_access.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def cursor_for(d, page):
    return hashlib.sha1(f"{d}|{page}|l4bench".encode()).hexdigest()[:12]


def gen_page(d, page):
    """确定性生成一页记录（含脏数据）。返回 (records, clean_stats)。"""
    rng = random.Random(f"{d}#{page}")
    recs, clean_amount, clean_qty, anomalies = [], 0.0, 0, 0
    base = DATES.index(d) * 1000 + page * 100
    for i in range(PER_PAGE):
        rid = f"R{base + i:06d}"
        qty = rng.randrange(1, 12)
        amount = round(qty * rng.uniform(15, 260), 2)
        r = {"record_id": rid, "ts": f"{d}T{rng.randrange(8,21):02d}:{rng.randrange(0,60):02d}:00",
             "store_id": rng.choice(STORES), "sku": rng.choice(SKUS),
             "qty": qty, "amount": amount, "status": "ok"}
        # --- 脏数据注入：D2/page2 ---
        if d == DATES[1] and page == 2:
            if i % 8 == 3:                      # 金额为带千分位的字符串
                r["amount"] = f"{amount:,.2f}"
            elif i % 11 == 5:                   # 数量缺失 -> 异常记录
                r["qty"] = None
                r["status"] = "incomplete"
        # --- 脏数据注入：D3/page3 重复 record_id ---
        if d == DATES[2] and page == 3 and i in (7, 19):
            r["record_id"] = f"R{base:06d}"
        recs.append(r)

    seen, dedup = set(), []
    for r in recs:
        if r["record_id"] in seen:
            continue
        seen.add(r["record_id"])
        dedup.append(r)
    for r in dedup:
        if r["qty"] is None:
            anomalies += 1
            continue
        amt = float(str(r["amount"]).replace(",", "")) if isinstance(r["amount"], str) else r["amount"]
        clean_amount += amt
        clean_qty += r["qty"]
    return recs, {"records_after_dedup": len(dedup), "anomalies": anomalies,
                  "valid_records": len(dedup) - anomalies,
                  "amount": round(clean_amount, 2), "qty": clean_qty}


def fault(d, page, params):
    """返回 (status, body_override) 或 None 表示正常。"""
    key = f"{d}|{page}"
    n = STATE["counts"].get(key, 0) + 1
    STATE["counts"][key] = n
    if d == DATES[2]:
        STATE["d3_touched"] = True
    save_state()

    if d == DATES[0] and page == 2 and n <= 2:
        return 500, '{"error":"upstream_timeout","retryable":true}'
    if d == DATES[0] and page == 3 and n == 1:
        return 429, '{"error":"rate_limited","retry_after":2}'
    if d == DATES[1] and page == 3 and not STATE["d3_touched"]:
        return 500, '{"error":"data_pipeline_unavailable","retryable":true,"hint":"该日该页数据尚未就绪"}'
    if d == DATES[2] and page == 1 and n == 1:
        return 200, '{"date":"%s","page":1,"total_pages":3,"records":[{"record_id":"R2000' % d
    if d == DATES[3] and page == 2 and params.get("cursor", [""])[0] != cursor_for(d, 1):
        return 400, '{"error":"missing_or_invalid_cursor","hint":"page2 需要 page1 返回的 next_cursor"}'
    return None


class H(BaseHTTPRequestHandler):
    server_version = "L4MockAPI/1.0"

    def log_message(self, *a):
        pass

    def _send(self, code, body, headers=None):
        b = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/api/v1/health":
            self._send(200, json.dumps({"status": "ok"})); return
        if u.path == "/api/v1/dates":
            self._send(200, json.dumps({"dates": DATES})); return
        if u.path != "/api/v1/records":
            self._send(404, '{"error":"not_found"}'); return

        d = q.get("date", [""])[0]
        try:
            page = int(q.get("page", ["1"])[0])
        except ValueError:
            page = 0
        if d not in DATES or not (1 <= page <= PAGES):
            log({"date": d, "page": page, "status": 400, "note": "bad_params"})
            self._send(400, '{"error":"bad_params","valid_dates":%s,"pages":%d}'
                       % (json.dumps(DATES), PAGES)); return

        f = fault(d, page, q)
        if f:
            code, body = f
            log({"date": d, "page": page, "status": code,
                 "attempt": STATE["counts"][f"{d}|{page}"], "injected": True})
            hdr = {"Retry-After": "2"} if code == 429 else {}
            self._send(code, body, hdr); return

        recs, _ = gen_page(d, page)
        payload = {"date": d, "page": page, "total_pages": PAGES,
                   "next_cursor": cursor_for(d, page) if page < PAGES else None,
                   "records": recs}
        log({"date": d, "page": page, "status": 200,
             "attempt": STATE["counts"][f"{d}|{page}"], "records": len(recs)})
        self._send(200, json.dumps(payload, ensure_ascii=False))


def write_ground_truth():
    GT.mkdir(exist_ok=True)
    days, total = {}, {"valid_records": 0, "anomalies": 0, "amount": 0.0, "qty": 0}
    for d in DATES:
        agg = {"valid_records": 0, "anomalies": 0, "amount": 0.0, "qty": 0,
               "records_after_dedup": 0}
        for p in range(1, PAGES + 1):
            _, s = gen_page(d, p)
            for k in agg:
                agg[k] += s[k]
        agg["amount"] = round(agg["amount"], 2)
        days[d] = agg
        for k in total:
            total[k] += agg[k]
    total["amount"] = round(total["amount"], 2)
    (GT / "expected_totals.json").write_text(
        json.dumps({"dates": DATES, "pages_per_day": PAGES, "per_day": days,
                    "overall": total}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[ok] ground truth -> {GT/'expected_totals.json'}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8741)
    ap.add_argument("--reset", action="store_true", help="清空 logs/（含故障进度）")
    a = ap.parse_args()
    if a.reset:
        shutil.rmtree(LOGS, ignore_errors=True)
    LOGS.mkdir(exist_ok=True)
    load_state()
    write_ground_truth()
    print(f"L4 模拟 API 已启动: http://127.0.0.1:{a.port}/api/v1/records?date={DATES[0]}&page=1")
    ThreadingHTTPServer(("127.0.0.1", a.port), H).serve_forever()


if __name__ == "__main__":
    main()
