"""L2 测试表单站：5 页联动表单 + 服务端校验 + 故障注入。纯标准库，无依赖。

启动：
    python server.py            # http://127.0.0.1:8731
    python server.py --port X --reset

行为要点（评分依赖，改动请同步 score.py）：
  * 全服务端渲染，无 JS 依赖，浏览器 Agent / HTTP Agent 都能做。
  * 第 3 页（城市）首次提交必定返回「系统繁忙」瞬时错误，考察重试。
  * 第 4 页收入字段拒绝千分位逗号和「元」字，考察数据清洗。
  * 第 5 页需要填写第 4 页页面上出现的回执码，考察跨页记忆。
  * 每次 POST 都记入 logs/events.jsonl；最终提交写 logs/submission.json。
"""
import argparse
import html
import json
import re
import shutil
import time
import uuid
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parent
LOGS = ROOT / "logs"
SESSIONS: dict[str, dict] = {}

PROVINCES = {"31": "上海市", "11": "北京市", "44": "广东省", "32": "江苏省"}
CITIES = {
    "31": [("31-01", "黄浦区"), ("31-02", "徐汇区"), ("31-03", "浦东新区"), ("31-04", "静安区")],
    "11": [("11-01", "海淀区"), ("11-02", "朝阳区"), ("11-03", "西城区")],
    "44": [("44-01", "广州市"), ("44-02", "深圳市"), ("44-03", "珠海市")],
    "32": [("32-01", "南京市"), ("32-02", "苏州市"), ("32-03", "无锡市")],
}
JOBS = [("engineer", "工程师"), ("manager", "管理岗"), ("sales", "销售"), ("other", "其他")]
STEP_TITLES = {1: "个人信息", 2: "所在省份", 3: "详细地址", 4: "工作与收入", 5: "确认提交"}


def log_event(rec: dict) -> None:
    LOGS.mkdir(exist_ok=True)
    rec["ts"] = datetime.now().isoformat(timespec="seconds")
    with (LOGS / "events.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# --------------------------------------------------------------- 校验
def v_step1(d):
    e = {}
    name = d.get("full_name", "").strip()
    if len(name) < 2:
        e["full_name"] = "姓名至少 2 个字符"
    idn = d.get("id_number", "").strip()
    if not re.fullmatch(r"\d{17}[\dXx]", idn):
        e["id_number"] = "身份证号必须为 18 位，不能包含空格或其他分隔符"
    phone = d.get("phone", "").strip()
    if not re.fullmatch(r"1\d{10}", phone):
        e["phone"] = "手机号必须是 11 位数字，不要国家码、空格或连字符"
    email = d.get("email", "").strip()
    if not re.fullmatch(r"[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}", email):
        e["email"] = "邮箱格式无效（本系统只接受全小写邮箱）"
    bd = d.get("birth_date", "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", bd):
        e["birth_date"] = "出生日期格式必须为 YYYY-MM-DD"
    elif not e.get("id_number") and idn[6:14] != bd.replace("-", ""):
        e["birth_date"] = "出生日期与身份证号中的出生日期不一致"
    return e


def v_step2(d):
    return {} if d.get("province") in PROVINCES else {"province": "请选择省份"}


def v_step3(d, prov):
    e = {}
    valid = {c for c, _ in CITIES.get(prov, [])}
    if d.get("city") not in valid:
        e["city"] = "请选择与所选省份匹配的城市/区"
    if not re.fullmatch(r"\d{6}", d.get("postcode", "").strip()):
        e["postcode"] = "邮政编码必须是 6 位数字"
    if len(d.get("street", "").strip()) < 8:
        e["street"] = "详细地址至少 8 个字符"
    return e


def v_step4(d, birth):
    e = {}
    if len(d.get("employer", "").strip()) < 4:
        e["employer"] = "单位名称至少 4 个字符"
    if d.get("job_title") not in {j for j, _ in JOBS}:
        e["job_title"] = "请选择岗位类型"
    inc = d.get("monthly_income", "").strip()
    if not re.fullmatch(r"\d+", inc):
        e["monthly_income"] = "月收入只能填纯数字（不要千分位逗号、货币符号或单位）"
    elif not (1000 <= int(inc) <= 1000000):
        e["monthly_income"] = "月收入超出合理范围 1000-1000000"
    sd = d.get("start_date", "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", sd):
        e["start_date"] = "入职日期格式必须为 YYYY-MM-DD"
    else:
        try:
            s = date.fromisoformat(sd)
            b = date.fromisoformat(birth)
            if s > date.today():
                e["start_date"] = "入职日期不能晚于今天"
            elif (s - b).days < 16 * 365:
                e["start_date"] = "入职时年龄不足 16 周岁，请核对"
        except ValueError:
            e["start_date"] = "日期无效"
    return e


def v_step5(d, sess):
    e = {}
    if d.get("receipt_code", "").strip().upper() != sess["receipt_code"]:
        e["receipt_code"] = "回执码不正确（见第 4 页页面提示）"
    if d.get("declaration") != "on":
        e["declaration"] = "必须勾选真实性声明"
    return e


# --------------------------------------------------------------- 渲染
def page(step, sess, errors=None, banner=""):
    errors = errors or {}
    d = sess["data"]
    err = lambda k: f'<p class="err" id="err_{k}">✗ {html.escape(errors[k])}</p>' if k in errors else ""
    val = lambda k: html.escape(d.get(k, ""))
    body = ""

    if step == 1:
        body = f"""
        <label>姓名 <input name="full_name" value="{val('full_name')}"></label>{err('full_name')}
        <label>身份证号 <input name="id_number" value="{val('id_number')}"></label>{err('id_number')}
        <label>手机号 <input name="phone" value="{val('phone')}"></label>{err('phone')}
        <label>电子邮箱 <input name="email" value="{val('email')}"></label>{err('email')}
        <label>出生日期 <input name="birth_date" placeholder="YYYY-MM-DD" value="{val('birth_date')}"></label>{err('birth_date')}"""
    elif step == 2:
        opts = "".join(
            f'<option value="{c}"{" selected" if d.get("province") == c else ""}>{n}</option>'
            for c, n in PROVINCES.items())
        body = f'<label>省份 <select name="province"><option value="">请选择</option>{opts}</select></label>{err("province")}'
    elif step == 3:
        prov = d.get("province", "")
        opts = "".join(
            f'<option value="{c}"{" selected" if d.get("city") == c else ""}>{n}</option>'
            for c, n in CITIES.get(prov, []))
        body = f"""<p class="hint">当前省份：{PROVINCES.get(prov, '未选择')}（城市选项随省份联动）</p>
        <label>城市/区 <select name="city"><option value="">请选择</option>{opts}</select></label>{err('city')}
        <label>邮政编码 <input name="postcode" value="{val('postcode')}"></label>{err('postcode')}
        <label>详细地址 <input name="street" size="50" value="{val('street')}"></label>{err('street')}"""
    elif step == 4:
        opts = "".join(
            f'<option value="{c}"{" selected" if d.get("job_title") == c else ""}>{n}</option>'
            for c, n in JOBS)
        body = f"""<p class="hint">本次申请回执码：<b>{sess['receipt_code']}</b>（最后一页需要填写，请记录）</p>
        <label>工作单位 <input name="employer" size="40" value="{val('employer')}"></label>{err('employer')}
        <label>岗位类型 <select name="job_title"><option value="">请选择</option>{opts}</select></label>{err('job_title')}
        <label>月收入(元) <input name="monthly_income" value="{val('monthly_income')}"></label>{err('monthly_income')}
        <label>入职日期 <input name="start_date" placeholder="YYYY-MM-DD" value="{val('start_date')}"></label>{err('start_date')}"""
    elif step == 5:
        rows = "".join(f"<tr><td>{html.escape(k)}</td><td>{html.escape(str(v))}</td></tr>"
                       for k, v in d.items())
        body = f"""<table class="review">{rows}</table>
        <label>回执码 <input name="receipt_code" value="{val('receipt_code')}"></label>{err('receipt_code')}
        <label><input type="checkbox" name="declaration"> 我声明以上信息真实有效</label>{err('declaration')}"""

    nav = '<a href="/step/%d">← 上一步</a>' % (step - 1) if step > 1 else ""
    return f"""<!doctype html><meta charset="utf-8"><title>第{step}步 · {STEP_TITLES[step]}</title>
<style>body{{font-family:sans-serif;max-width:680px;margin:2rem auto}}label{{display:block;margin:.8rem 0}}
input,select{{padding:.35rem;margin-left:.4rem}}.err{{color:#c00;margin:.2rem 0 .6rem}}
.banner{{background:#fee;border:1px solid #c00;padding:.6rem;margin:1rem 0}}
.hint{{background:#eef;padding:.5rem}}table.review td{{border:1px solid #ccc;padding:.3rem .6rem}}</style>
<h1>信息登记表 — 第 {step}/5 步：{STEP_TITLES[step]}</h1>
{f'<div class="banner">{html.escape(banner)}</div>' if banner else ''}
<form method="post" action="/step/{step}">{body}
<p><button type="submit">{'提交申请' if step == 5 else '下一步'}</button> {nav}</p></form>"""


DONE = """<!doctype html><meta charset="utf-8"><title>提交成功</title>
<body style="font-family:sans-serif;max-width:680px;margin:2rem auto">
<h1>提交成功</h1><p>受理编号：<b>{ref}</b></p><p>状态：ACCEPTED</p>
<p>本次共触发 {errs} 次校验错误。</p></body>"""


# --------------------------------------------------------------- HTTP
class H(BaseHTTPRequestHandler):
    server_version = "L2FormBench/1.0"

    def log_message(self, *a):
        pass

    def _sess(self):
        sid = None
        cookie = self.headers.get("Cookie", "")
        m = re.search(r"sid=([0-9a-f\-]+)", cookie)
        if m and m.group(1) in SESSIONS:
            sid = m.group(1)
        if sid is None:
            sid = str(uuid.uuid4())
            SESSIONS[sid] = {"data": {}, "error_count": 0, "attempts": {},
                             "fail_injected": False, "started": time.time(),
                             "receipt_code": "RC-" + uuid.uuid4().hex[:6].upper()}
            log_event({"event": "session_start", "sid": sid})
        return sid, SESSIONS[sid]

    def _send(self, body, code=200, sid=None):
        b = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        if sid:
            self.send_header("Set-Cookie", f"sid={sid}; Path=/")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        sid, sess = self._sess()
        path = self.path.split("?")[0]
        if path in ("/", "/step", "/step/"):
            self._send(page(1, sess), sid=sid); return
        m = re.fullmatch(r"/step/([1-5])", path)
        if m:
            self._send(page(int(m.group(1)), sess), sid=sid); return
        if path == "/health":
            self._send("ok", sid=sid); return
        self._send("<h1>404</h1>", 404, sid=sid)

    def do_POST(self):
        sid, sess = self._sess()
        m = re.fullmatch(r"/step/([1-5])", self.path.split("?")[0])
        if not m:
            self._send("<h1>404</h1>", 404, sid=sid); return
        step = int(m.group(1))
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n).decode("utf-8")
        d = {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}
        sess["attempts"][str(step)] = sess["attempts"].get(str(step), 0) + 1

        # 故障注入：第 3 页首次提交返回瞬时错误
        if step == 3 and not sess["fail_injected"]:
            sess["fail_injected"] = True
            sess["data"].update(d)
            log_event({"event": "injected_failure", "sid": sid, "step": 3})
            self._send(page(3, sess, {}, "系统繁忙（错误码 E503），本次提交未保存，请稍后重试。"),
                       503, sid=sid)
            return

        if step == 1:
            errs = v_step1(d)
        elif step == 2:
            errs = v_step2(d)
        elif step == 3:
            errs = v_step3(d, sess["data"].get("province", ""))
        elif step == 4:
            errs = v_step4(d, sess["data"].get("birth_date", "1900-01-01"))
        else:
            errs = v_step5(d, sess)

        sess["data"].update({k: v for k, v in d.items() if k != "declaration"})
        log_event({"event": "submit", "sid": sid, "step": step,
                   "attempt": sess["attempts"][str(step)],
                   "errors": list(errs), "ok": not errs})

        if errs:
            sess["error_count"] += len(errs)
            self._send(page(step, sess, errs, "表单校验未通过，请修正下列字段。"), 422, sid=sid)
            return

        if step < 5:
            self._send(page(step + 1, sess), sid=sid)
            return

        ref = "APP-" + uuid.uuid4().hex[:8].upper()
        LOGS.mkdir(exist_ok=True)
        payload = {"ref": ref, "sid": sid, "status": "ACCEPTED",
                   "submitted_at": datetime.now().isoformat(timespec="seconds"),
                   "duration_sec": round(time.time() - sess["started"], 1),
                   "error_count": sess["error_count"], "attempts": sess["attempts"],
                   "receipt_code": sess["receipt_code"], "data": sess["data"]}
        (LOGS / "submission.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        log_event({"event": "accepted", "sid": sid, "ref": ref,
                   "error_count": sess["error_count"]})
        self._send(DONE.format(ref=ref, errs=sess["error_count"]), sid=sid)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8731)
    ap.add_argument("--reset", action="store_true", help="清空 logs/ 后启动")
    a = ap.parse_args()
    if a.reset:
        shutil.rmtree(LOGS, ignore_errors=True)
    LOGS.mkdir(exist_ok=True)
    print(f"L2 表单站已启动: http://127.0.0.1:{a.port}/  (Ctrl+C 退出)")
    ThreadingHTTPServer(("127.0.0.1", a.port), H).serve_forever()


if __name__ == "__main__":
    main()
