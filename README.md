# 扫描期版质检台

面向真实数据复核的中型数据工程工具，包含六个可独立调用的业务模块。规则、失败原因与证据字段见 docs/DATA-DICTIONARY.md，陷阱和测试映射见 docs/DEPTH.md。

## 启动

docker compose up --build

浏览器访问 http://localhost:18217。测试：docker compose exec -T app pytest -q

## 模块

- M1 清单采信：页面 /intake，接口 /api/scans/import
- M2 期版契约：页面 /issues，接口 /api/issues/normalize
- M3 页码序列：页面 /pages，接口 /api/pages/check
- M4 重扫判定：页面 /rescans，接口 /api/rescans/evaluate
- M5 混期归因：页面 /mixed，接口 /api/mixed/evaluate
- M6 批次对账：页面 /batches，接口 /api/reconcile
- M7 离线同步：页面 /sync，接口 /api/sync/batches

## 运行规则

同一输入内容生成相同运行标识，重复执行复用已有批次。隔离记录包含原因码、原始行号与原值；业务导出只保留一份证据清单。

## 离线同步（M7）

站点联网后把真实清单与离线修订提交到 `POST /api/sync/batches`；`station_id` + 载荷摘要构成批次幂等键，重试不会重复接收扫描件。

- 合并：以修订声明的共同底稿做三方合并，双方互不冲突的字段直接入账。
- 挂起：同字段分歧或改动中心已核准的期版页归属时，仅该字段挂起并保留 base/ours/theirs 三方证据，其余字段照常入账。
- 裁定：`POST /api/sync/conflicts/{conflict_id}/resolve` 逐字段裁定后入账；记录版本已前进时拒绝过期裁定。
- 前序：缺少前序修订的修订暂存（STAGED），前序属于其他记录或未入账时拒绝自动合并（PARENT_DIVERGED）。
- 恢复：`POST /api/sync/recover` 只重放未入账修订，可使用 batch_id 或 revision_id 定位。
- 并发：中心复核 `POST /api/sync/reviews` 与站点同步均带版本前提，过期版本返回 409。
- 回读：`GET /api/sync/batches/{id}`、`GET /api/sync/records/{business_key}`、`GET /api/sync/scans`、`GET /api/sync/quarantine`、`GET /api/sync/progress` 核对站点修订进度与已入账扫描。

M7 不改变重扫优选结论，也不做站点派工。

