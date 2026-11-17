# 数据字典

- run_id：规则版本与输入 SHA-256 前 12 位组成的稳定批次标识。
- line_no：从 1 开始的物理来源行号，不因隔离或排序改变。
- business_key：本项目声明的业务唯一键，始终按字符串存储。
- reason_code：隔离原因的稳定英文编码。
- accepted：进入业务计算的记录数。
- quarantined：保留证据但不参与业务计算的记录数。
- 数值：金额、税额、用量和权重使用 Decimal；对外序列化为字符串。
- 时间：存储为带偏移的 ISO 8601，分桶使用半开区间。
- 空值：缺失键、空串和业务哨兵分别记录。
- 幂等：完整输入字节与规则版本相同则复用 run_id，不新增结果。
- revision_id：站点本地修订号，入库时前缀站点标识（`station_id|revision_id`）保证全局唯一。
- base_version：修订所依据的共同底稿版本；缺失时以当前入账版本为底稿。
- expected_version：提报方声明的当前版本；与入账版本不符即判过期，不合并。
- record version：中心记录入账版本号，每次入账 +1；同一版本快照不可变。
- approved_fields / approved_values：中心复核已核准的字段及当时取值，未裁定前不接受改动。
- conflict：挂起字段的三方证据 base / ours / theirs，裁定后记录 resolution_source 与裁定人。

