# L3b 任务 Prompt — 脏数据清洗与分析报告

> 交付给被测 Agent 的原文从分隔线开始。`_ground_truth/` 请移出 Agent 可见范围。

---

工作目录：`<替换为 L3b_data_analysis/workspace 的绝对路径>`

`orders_raw.csv`（与 `orders_raw.xlsx` 同内容）是三套业务系统合并导出的订单流水，含缺失值、重复值、异常值和不一致的格式。字段说明见 `data_dictionary.md`。

请完成清洗、分析、出图、写报告。**统计截止日：2026-07-31。**

## 一、清洗规则（严格执行，不要自行改口径）

按以下顺序处理，**每条被剔除的记录只归因于第一个命中的原因**：

1. **重复**：整行完全重复（所有字段一致）的记录，只保留文件中**第一次出现**的那条，其余标记 `duplicate` 剔除。
2. **缺失**：`quantity` 或 `unit_price` 为空 / `NA` / `N/A` / `null` / `-` / `未知` → 标记 `missing_value` 剔除，**不要插补**。
   `region` 缺失 → 不剔除，归一化为 `未知`。`discount_rate` 缺失 → 按 `0` 处理，不剔除。
3. **日期**：`order_date` 统一为 `YYYY-MM-DD`。已知输入格式有 `YYYY-MM-DD`、`YYYY/MM/DD`、`DD-MM-YYYY`、`YYYY年M月D日`。
   无法解析、或晚于 2026-07-31 的 → 标记 `invalid_date` 剔除。
4. **数值清洗**：`unit_price` 去掉 `¥`、千分位逗号和前后空格后转为数值。
5. **异常值**：
   - `quantity` ≤ 0 或 > 1000 → `outlier_quantity` 剔除
   - `unit_price` ≤ 0 → `outlier_price` 剔除
   - `discount_rate` 不在 [0, 0.9] 区间 → `outlier_discount` 剔除
6. **大区归一**：`华东/华东区/East/east` → `华东`；`华北/华北区/North` → `华北`；`华南/华南区/South` → `华南`；`西部/西区/West` → `西部`（忽略大小写与前后空格）。
7. **派生字段**：`revenue = quantity × unit_price × (1 − discount_rate)`，四舍五入保留 2 位小数。

## 二、需要计算的指标

- 有效订单数、被剔除总数、按原因分组的剔除数
- 总营收、平均订单金额（总营收 / 有效订单数）
- 分大区营收、分品类营收、分月营收（按 `YYYY-MM`）
- 营收 Top5 商品
- 去重后的客户数

## 三、交付物（全部放在工作目录下）

1. `cleaned.csv` — 清洗后的有效记录，UTF-8，含表头，至少包含：
   `order_id,order_date,region,channel,category,product,customer_id,quantity,unit_price,discount_rate,revenue`
2. `dropped.csv` — 被剔除的记录，含表头，至少包含 `order_id,drop_reason`（`drop_reason` 取值只能是上面 6 个标记之一）
3. `metrics.json` — 严格使用下列键名（数值用 number，不要写成字符串）：

```json
{
  "valid_orders": 0,
  "dropped_total": 0,
  "dropped_by_reason": {"duplicate": 0, "missing_value": 0, "invalid_date": 0,
                        "outlier_quantity": 0, "outlier_price": 0, "outlier_discount": 0},
  "total_revenue": 0.0,
  "avg_order_value": 0.0,
  "unique_customers": 0,
  "revenue_by_region": {"华东": 0.0},
  "revenue_by_category": {"数码": 0.0},
  "revenue_by_month": {"2026-01": 0.0},
  "top5_products": [{"product": "", "revenue": 0.0}]
}
```

4. **图表**（至少 3 张，保存为 PNG 或 SVG）：
   - `chart_monthly.png` 分月营收趋势
   - `chart_region.png` 分大区营收对比
   - `chart_category.png` 分品类营收占比
   图表须有标题、坐标轴标签、单位；中文不得显示为方框。
5. `analysis.py`（或 `.ipynb`） — 完整可重跑的分析脚本。要求：**从 `orders_raw.csv` 出发，一次运行即可重新生成上面全部产物**，不依赖任何手工中间步骤，不硬编码结果数字。
6. `report.md` — 分析报告，包含：
   - 数据质量小结（原始行数、各类问题数量及占比）
   - 关键结论（至少 4 条，每条都要引用你算出的具体数字，并说明数字来自哪张图/哪个指标）
   - 图表引用（用 `![](chart_xxx.png)` 嵌入）
   - **口径与局限**：你做了哪些假设、哪些结论受清洗规则影响较大、数据不支持哪些结论

## 四、注意

- 如果环境里没有 pandas / matplotlib，你可以自行安装，或用标准库实现；无论哪种方式，`analysis.py` 必须能在干净环境下跑通，并在报告里写明依赖。
- 报告里的每个数字必须与 `metrics.json` 一致；不一致视为错误。
- 不要修改 `orders_raw.csv` / `orders_raw.xlsx`。
