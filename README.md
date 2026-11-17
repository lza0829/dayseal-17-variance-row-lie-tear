# Dayseal（日封台）

收银流水按营业日归集，日结封账后锁定；长短款可查。

- 前端 http://127.0.0.1:5700
- API http://127.0.0.1:10700

## 封账差额策略（全局唯一）

生效策略：**先处置再封**（`dispose_before_seal`）。系统只贯彻这一种策略，不按营业日/收银机混用；
「先封后挂 + pending」不在本代码库实现。

| 差额 | 封账要求 | 落库 disposition |
| --- | --- | --- |
| 0 | 直接封账（可附校正说明） | `balanced` |
| < 0（短款） | 必须当场认损 | `loss` |
| > 0（长款） | 必须当场认溢 | `gain` |
| 非零 | 或先补一笔真实流水（`correction_lines`）把差额补到 0 | `balanced` |

- 未处置 / 方向不符 / 零差却选认损认溢 / 用备注冒充处置 / `pending` —— 一律在写批次之前 400 拒绝：
  不生成封账批次、不写长短款行、不锁日。
- 批次、补录流水、长短款行、日锁定在同一事务写入；`sealed` 与处置行、日锁定同源。
- DB 触发器兜底：`sealed` 批次必须已有最终处置行；`variance_rows.disposition` 只允许
  `balanced/loss/gain`，且必须与差额方向一致；一个营业日最多一个 `sealed` 批次。
- 已封账营业日继续拒写流水（新增/修改/删除均 409）。
- 自检接口 `GET /api/close/integrity` 暴露「已 sealed 却无长短款行」「处置与差额方向不符」等问题。

处置记录不是备注文案：`variance_rows.disposition` 是状态字段，`note` 只是说明。

## 启动

```powershell
cd backend
py -3.11 -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\uvicorn app.main:app --host 127.0.0.1 --port 10700 --app-dir .
```

```powershell
cd frontend
npm install
npm run dev -- --host 127.0.0.1 --port 5700
```

## 测试

```powershell
cd backend
.\.venv\Scripts\pip install -r requirements-dev.txt
.\.venv\Scripts\python -m pytest tests -q
```
