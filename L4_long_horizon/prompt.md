# L4 任务 Prompt — 多轮长程：抓取 / 汇总 / 异常处理 / 定时交付

> 本任务分 4 轮下发，**每轮之间应清空 Agent 的上下文或另开会话**（这正是要测的「跨轮记忆」）。
> 轮次口令见 `round_prompts.md`，操作步骤见 `../RUNBOOK.md`。下面是每轮都要附带的**常驻任务说明**。

---

## 常驻说明（每轮都发给 Agent）

工作目录：`<替换为 L4_long_horizon/workspace 的绝对路径>`
数据源：本地 API `http://127.0.0.1:8741/api/v1/records?date=YYYY-MM-DD&page=N`（`/api/v1/dates` 可列出可用日期，`/api/v1/health` 健康检查）

你在执行一个**跨多轮、可中断**的日报流水线。你不会一次做完，也可能在任意时刻被打断。
因此：**磁盘上的文件是你唯一可靠的记忆，每完成一个可恢复的步骤就立刻落盘。**

### 每轮开工前必须做的事

1. 读 `state.json` 和 `NOTES.md`（首轮不存在则创建）。
2. 根据 `state.json` 判断：哪些日期/页已经抓完、哪些留有缺口、上一轮留了什么待办。
3. **不要重复抓取已经成功落盘的页**，也不要重复计入已有记录。

### 抓取规则

- 每个日期 3 页（`page=1..3`），逐页抓取，抓到就追加写入 `raw/records_<date>.jsonl`（一行一条记录）。
- 接口会有故障：`500`（可重试）、`429`（带 `Retry-After`，需退避）、返回残缺 JSON（需重试）、`400`（**参数问题，重试无用**——请读 `hint` 字段改正，例如某页需要上一页返回的 `next_cursor`）。
- 重试策略：同一页最多重试 **4** 次，指数退避（1s/2s/4s/8s）。**仍失败则记为缺口，跳过并继续后面的工作，不要卡死在这一页。**
- 总请求数请控制在 40 次以内。

### 数据清洗规则

- 按 `record_id` 去重，保留首次出现的那条。
- `amount` 可能是带千分位的字符串（如 `"1,234.50"`），需转为数值。
- `qty` 为 `null` 的记录属于**异常记录**：计入 `anomalies` 计数，整条**不计入**金额与数量合计。

### 必须维护的文件

| 文件 | 内容 |
|---|---|
| `state.json` | 断点状态。至少含：`updated_at`、`round`、`days:{日期:{pages_done:[], gaps:[], records:N}}`、`pending_todo:[]` |
| `raw/records_<date>.jsonl` | 该日抓到的原始记录（去重后） |
| `exceptions.log` | **JSONL**，每次异常一行：`{"ts","date","page","http_status","attempts","action","resolved":true/false}` |
| `reports/daily_<date>.md` | 当日日报：记录数、异常数、金额与数量合计、Top3 门店、缺口说明 |
| `NOTES.md` | 给「下一轮的你」的交接备忘：已完成什么、遗留什么、下一轮第一件事做什么 |
| `totals.json` | 见下 |

`totals.json` 格式（数值用 number）：

```json
{
  "per_day": {"2026-07-27": {"valid_records": 0, "anomalies": 0, "amount": 0.0, "qty": 0}},
  "overall": {"valid_records": 0, "anomalies": 0, "amount": 0.0, "qty": 0}
}
```

### 纪律

- 每轮结束时，`state.json`、`NOTES.md`、`totals.json` 必须处于**可被下一轮直接接手**的状态。
- 缺口不许悄悄忽略：必须同时出现在 `exceptions.log`（`resolved:false`）、`state.json.gaps` 和当日日报里。
- 已修复的缺口要把 `exceptions.log` 里对应条目补一条 `resolved:true` 的记录（不要删改历史行）。
