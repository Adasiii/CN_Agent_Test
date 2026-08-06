"""L2 自动评分：读取服务端的 submission.json 与 events.jsonl。

用法：
    python score.py                      # 日志用 ./logs，result.md 在脚本同级目录找
    python score.py --logs <日志目录>     # server.py 搬到别处时用
    python score.py --ws <目录>          # Agent 把 result.md 写到别处时用
"""
import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent

EXPECTED = {
    "full_name": "陈思远",
    "id_number": "310104199203071827",
    "phone": "13800138000",
    "email": "chen.siyuan@example.com",
    "birth_date": "1992-03-07",
    "province": "31",
    "city": "31-02",
    "postcode": "200030",
    "employer": "蓝川数据科技（上海）有限公司",
    "job_title": "engineer",
    "monthly_income": "18500",
    "start_date": "2019-09-02",
}
# 自由文本字段：只要求包含这些关键片段
FUZZY = {"street": ["宛平南路", "88", "502"]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--logs", default=str(ROOT / "logs"),
                    help="server.py 写日志的目录（含 submission.json / events.jsonl）")
    ap.add_argument("--ws", default=str(ROOT), help="查找 result.md 的目录")
    ap.add_argument("--json")
    a = ap.parse_args()
    LOGS = Path(a.logs).resolve()
    WS = Path(a.ws).resolve()

    res = {"metrics": {}, "errors": [], "notes": []}
    sub_f = LOGS / "submission.json"
    if not sub_f.exists():
        print(json.dumps({"score": 0, "errors": ["未产生 submission.json —— 表单从未成功提交"]},
                         ensure_ascii=False, indent=2))
        return

    sub = json.loads(sub_f.read_text(encoding="utf-8"))
    data = sub["data"]

    hit, detail = 0, {}
    for k, want in EXPECTED.items():
        got = (data.get(k) or "").strip()
        ok = got == want
        detail[k] = {"expected": want, "got": got, "ok": ok}
        hit += ok
    for k, frags in FUZZY.items():
        got = (data.get(k) or "").strip()
        ok = all(f in got for f in frags)
        detail[k] = {"expected_contains": frags, "got": got, "ok": ok}
        hit += ok
    total_fields = len(EXPECTED) + len(FUZZY)
    res["metrics"]["field_accuracy"] = round(hit / total_fields, 4)
    res["metrics"]["fields"] = detail
    res["metrics"]["submitted"] = sub["status"] == "ACCEPTED"
    res["metrics"]["ref"] = sub["ref"]
    res["metrics"]["duration_sec"] = sub["duration_sec"]

    events = []
    ev_f = LOGS / "events.jsonl"
    if ev_f.exists():
        events = [json.loads(l) for l in ev_f.read_text(encoding="utf-8").splitlines() if l.strip()]
    validation_fails = [e for e in events if e.get("event") == "submit" and e.get("errors")]
    injected = [e for e in events if e.get("event") == "injected_failure"]
    total_posts = len([e for e in events if e.get("event") == "submit"]) + len(injected)

    # 会话统计：一次干净的作业只需要 1 个会话；会话数多说明它在反复从头重来，
    # 而且每个走到第 5 步的会话都是一次「差点真的提交出去」的风险。
    sessions = len([e for e in events if e.get("event") == "session_start"])
    farthest = {}
    for e in events:
        if e.get("event") == "submit":
            sid = e.get("sid")
            farthest[sid] = max(farthest.get(sid, 0), e.get("step", 0))
    reached_final = sum(1 for v in farthest.values() if v >= 5)

    res["metrics"]["validation_failures"] = len(validation_fails)
    res["metrics"]["failed_fields"] = sorted({f for e in validation_fails for f in e["errors"]})
    res["metrics"]["injected_failures_hit"] = len(injected)
    res["metrics"]["total_post_requests"] = total_posts
    res["metrics"]["sessions"] = sessions
    res["metrics"]["sessions_reaching_final_step"] = reached_final
    # 注意：这是「最终成功的那个会话」的尝试次数，不含之前被放弃的会话
    res["metrics"]["winning_session_attempts"] = sub["attempts"]
    res["metrics"]["recovered_from_injected_failure"] = bool(injected) and sub["status"] == "ACCEPTED"
    res["metrics"]["result_md_exists"] = (WS / "result.md").exists()

    # 效率分 = 请求效率 6 : 会话效率 4
    #   请求：理论最优 6 次（5 步 + 1 次注入故障），≤8 满分，≥40 归零
    #   会话：≤2 个满分（允许一次重开），≥8 个归零
    clamp = lambda x: max(0.0, min(1.0, x))
    if total_posts:
        req_eff = clamp((40 - total_posts) / (40 - 8))
        sess_eff = clamp((8 - sessions) / (8 - 2)) if sessions else 1.0
        eff = 0.6 * req_eff + 0.4 * sess_eff
    else:
        req_eff = sess_eff = eff = 0.0
    res["metrics"]["efficiency"] = round(eff, 3)
    res["metrics"]["efficiency_detail"] = {"request": round(req_eff, 3), "session": round(sess_eff, 3)}

    score = (
        res["metrics"]["field_accuracy"] * 45
        + (20 if res["metrics"]["submitted"] else 0)
        + (10 if res["metrics"]["recovered_from_injected_failure"] else 0)
        + eff * 20
        + (5 if res["metrics"]["result_md_exists"] else 0)
    )
    res["score"] = round(score, 2)
    res["score_breakdown"] = "字段准确率45 + 成功提交20 + 故障恢复10 + 交互效率20 + 结果报告5"

    print(json.dumps(res, ensure_ascii=False, indent=2))
    print(f"\n=== L2 得分: {res['score']} / 100 ===")
    if a.json:
        Path(a.json).write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
