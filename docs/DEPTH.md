# 深度登记 扫描期版质检台

- 规模：中型
- 核心规则：期号、版次、页码构成业务键，图像相似只生成候选，页眉证据决定混期。
- 使用者与工作流：质检员导入扫描清单，核对期版与页序，裁决重扫和混期，归档证据。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 清单采信 | 导入扫描清单并定位坏行 | POST /api/scans/import | `app/modules/m1_intake/parser.py` `app/modules/m1_intake/integrity.py` | `tests/test_m1_intake.py` |
| M2 期版契约 | 规范期号、版次和页码 | POST /api/issues/normalize | `app/modules/m2_issue/rules.py` `app/modules/m2_issue/catalog.py` | `tests/test_m2_issue.py` |
| M3 页码序列 | 查缺页、重页与越界页 | POST /api/pages/check | `app/modules/m3_pages/sequence.py` `app/modules/m3_pages/reconcile.py` | `tests/test_m3_pages.py` |
| M4 重扫判定 | 比较扫描候选并保留清晰版本 | POST /api/rescans/evaluate | `app/modules/m4_rescans/similarity.py` `app/modules/m4_rescans/decision.py` | `tests/test_m4_rescans.py` |
| M5 混期归因 | 用页眉和页码证据裁决混期 | POST /api/mixed/evaluate | `app/modules/m5_mixed/evidence.py` `app/modules/m5_mixed/conflicts.py` | `tests/test_m5_mixed.py` |
| M6 批次对账 | 检查四账并导出证据 | POST /api/reconcile | `app/modules/m6_batches/compare.py` `app/modules/m6_batches/exporter.py` | `tests/test_m6_batches.py` |
| M7 离线同步 | 接收站点真实清单与离线修订，合并、挂起裁定并回读 | POST /api/sync/batches | `app/modules/m7_sync/intake.py` `app/modules/m7_sync/model.py` `app/modules/m7_sync/service.py` | `tests/test_m7_sync.py` `tests/test_m7_sync_rules.py` `tests/test_m7_sync_http.py` |

## 陷阱登记

| TRAP | 现象 | 朴素实现会怎样 | 本项目处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-02 | 首列带 BOM | 期号字段取不到 | 读取表头时移除 BOM | `app/modules/m1_intake/parser.py` | `tests/test_m1_intake.py::test_strip_bom_header` |
| TRAP-05 | 行列数不齐 | 错位行进入页序 | 隔离并保留物理行号 | `app/modules/m1_intake/parser.py` | `tests/test_m1_intake.py::test_column_count_goes_quarantine` |
| TRAP-07 | 页眉为空 | 空串被当作一致 | 返回未知并转人工复核 | `app/modules/m5_mixed/evidence.py` | `tests/test_m5_mixed.py::test_missing_header_is_unknown` |
| TRAP-08 | 前导零页码 | 转整数后无法复核原编号 | 规范为三位字符串 | `app/modules/m2_issue/rules.py` | `tests/test_m2_issue.py::test_page_zero_pads_without_numeric_roundtrip` |
| TRAP-09 | 大摘要或相似哈希 | 数字往返或只凭相似度误判 | 摘要保形，相似仅生成候选 | `app/modules/m5_mixed/conflicts.py` | `tests/test_m5_mixed.py::test_hash_candidate_without_header_needs_review` |
| TRAP-14 | 同版多扫描值冲突 | 后写覆盖清晰版本 | 版本、清晰度、时间稳定择优 | `app/modules/m3_pages/sequence.py` | `tests/test_m3_pages.py::test_duplicate_page_detected` |
| TRAP-16 | 清单尾行截断 | 半行被当成扫描件 | 完整尾行检查并隔离 | `app/modules/m1_intake/integrity.py` | `tests/test_m1_intake.py::test_truncated_tail_detected` |
| TRAP-19 | 候选顺序改变 | 先到者决定保留项 | 显式排序键保持顺序无关 | `app/modules/m3_pages/sequence.py` | `tests/test_m3_pages.py::test_selection_order_independent` |
| TRAP-22 | 同内容多别名 | 合并顺序改变代表扫描 | 清晰度后以时间和文件名择一 | `app/modules/m4_rescans/decision.py` | `tests/test_m4_rescans.py::test_selection_is_deterministic_on_tie` |
| TRAP-26 | 陈旧重扫晚提交 | 旧版本覆盖新版 | 版本优先于输入顺序 | `app/modules/m3_pages/sequence.py` | `tests/test_m3_pages.py::test_higher_version_wins_not_later_input` |
| TRAP-31 | 旧底稿把中心已核准的期版页归属盖回去 | 整单覆盖丢复核，整单拒收丢新扫描 | 已核准字段独立保护，冲突字段挂起，其余字段和扫描照常入账 | `app/modules/m7_sync/model.py` `app/modules/m7_sync/service.py` | `tests/test_m7_sync.py::test_old_base_cannot_regress_approved_page` |
| TRAP-32 | 站点缺前序修订或前序分叉 | 直接合并产生幽灵版本 | 缺前序暂存 STAGED，分叉拒绝 PARENT_DIVERGED | `app/modules/m7_sync/service.py` | `tests/test_m7_sync.py::test_missing_parent_is_staged_not_merged` `tests/test_m7_sync.py::test_forked_parent_refuses_automatic_merge` |
| TRAP-33 | 同步中断后整批重传 | 扫描件重复接收 | 批次与修订双重幂等，恢复只重放未入账修订 | `app/modules/m7_sync/service.py` | `tests/test_m7_sync.py::test_interrupted_batch_can_resume_from_unbooked_point` |
| TRAP-34 | 中心复核与站点同步并发 | 过期版本覆盖新复核 | 版本前提不符直接拒绝 STALE_VERSION | `app/modules/m7_sync/service.py` | `tests/test_m7_sync_http.py::test_concurrent_review_and_sync_do_not_double_book` |

## 对账规则

- 应有页数 = 唯一采纳页 + 缺页数。
- 扫描件数 = 唯一采纳 + 被替代重扫 + 混期 + 隔离。
- 同一输入与规则版本重跑，运行标识和结果行数不变。

## 已知不覆盖

- 不读取图像二进制，感知哈希与 OCR 页眉由上游扫描软件提供。
- M7 只负责接收与合并站点修订，沿用 M4 的重扫优选结论，不在同步时重选扫描版本。
- M7 不做站点派工与任务分派，批次只由站点主动提交。

