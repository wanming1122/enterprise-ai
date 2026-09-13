# Changelog

All notable changes to this project will be documented in this file.

## [检索评测基建] - 2026-09-13

### Added

- `backend/evals/`：RAG 检索评测离线包——语料快照导出、55 题金标集（事实 / 多切片 / 跨文件 / 同义改写 + 语料外负例）、五项 IR 指标（Recall / Precision / HitRate / MRR / NDCG，纯 stdlib 实现）、B0~B4 消融跑批 CLI（逐题明细 + Markdown 报告）、LLM 起草候选问题、模型配置初始化脚本（智谱 rerank / DeepSeek 连通探针与 Fernet 加密入库）
- `backend/tests/test_crag_merge.py` / `test_embedding_grouping.py`：CRAG 轮次合并与查询向量分组的纯函数单测（8 用例）

### Changed

- `kb_rag_service`：新增 `retrieve_with_stages` 阶段化检索（返回向量 / BM25 / 融合 / 重排各阶段完整候选，生产 `retrieve` 委托之）；查询向量按（embedding 模型, 维度）分组嵌入，多库检索调用次数 10 → 分组数（检索 p50 5.6s → 1.1s）；CRAG 改写重检由「整体替换」改为「原查询加权并集」（`merge_round_results`），修复改写漂移挤占已命中切片的问题
- 模型配置：新增智谱 rerank（实测净负增益，默认关闭）与 DeepSeek 备用生成模型（探针通过入库，不设默认）

### Validated

- 金标集实测（全库联合检索、10 库 103 切片）：Recall@6 0.98 / NDCG@6 0.90 / MRR@6 0.90；四份评测报告与逐题明细见 `backend/evals/reports/`
- 测试 144 → 152 用例，全部通过

## [Agent P0 加固] - 2026-09-13

### Fixed

- **AI 问答连接池耗尽风险**：`chat_sse` 由全程持有 DB 会话重构为两段短事务，图执行与 LLM 流式期间不占连接；连接池显式 `pool_size=10, max_overflow=20`（此前默认 5+10，约 15 人并发流式问答即拖垮全站接口）
- **流式重试重复输出**：`_generate_node` 新增 `emitted` 守卫，增量已下发后禁止静默重试（预算降级/剥图/空回答三条重试路径），防止流中途失败时用户看到重复内容
- **SSE 反代空闲断流**：新增 `with_heartbeat` 心跳包裹器（静默超 15s 插入 `": ping"` 注释行），前端零改动兼容；断连取消行为与原直连一致
- **工具失败静默**：retrieve/nl2sql/server_admin 失败路径与 Chroma 向量删除补齐 warning/exception 日志，生产排障可追溯

### Added

- `app/utils/sse.py`：SSE 心跳包裹器（事件与心跳竞争等待）
- `tests/test_sse_heartbeat.py`：4 个心跳单测（透传 / 静默插入 / 等待期取消传播 / yield 边界关闭清理）

### Changed

- 测试 140 → 144 用例；详细评估与 P1/P2 改进计划见《Bug审计与修复记录.md》第九节

---

## [M11] - 2026-09-09

### Added

- **NL2SQL扩展业务表**
  - 新增对`sys_user`（员工）、`att_record`（考勤）、`sal_payroll`（工资单）表的查询支持
  - 支持跨表JOIN查询，如"技术部上月迟到几次"
  - 更新AI助手工具描述，引导正确使用新表

- **数据范围权限**
  - 角色表新增`data_scope`字段（1仅本人 2本部门 3全部）
  - 新增`get_data_scope()`和`apply_data_scope()`工具函数
  - 考勤、薪资列表接口集成数据范围过滤
  - 角色管理页面增加数据范围选择

- **AI错误类型区分**
  - 新增ErrorType常量（NETWORK/TIMEOUT/AUTH/SERVER/ABORT/UNKNOWN）
  - 根据错误类型显示不同图标和操作建议
  - 网络错误显示WiFi图标，其他错误显示感叹号图标

- **Dashboard扩展**
  - 新增薪资成本趋势图表（柱状图+折线图）
  - 新增各部门薪资分布饼图
  - 新增人力结构分析（性别+年龄双饼图）
  - 新增职位人数TOP8横向柱状图

- **面包屑导航**
  - 新增Breadcrumb组件，基于菜单树动态生成
  - 面包屑显示在内容区顶部

- **Header快捷菜单**
  - 下拉菜单增加"个人资料"和"修改密码"快捷入口
  - 支持用户头像显示

### Fixed

- AIModelOption类型缺少context_window字段
- MemoryDrawer.tsx类型错误
- MessageItem.test.tsx测试用例缺少id字段

### Changed

- NL2SQL可查询表从1张扩展到4张
- 角色Schema增加data_scope字段
- 考勤/薪资服务增加current_user参数

### Migration

- 数据库迁移：`e1f2a3b4c5d6_m11_data_scope.py`
  - sys_role表新增data_scope字段，默认值3（全部）

---

## [M10] - 2026-09-09

### Added

- AI助手用量监控：ai_message增加usage统计列

---

## Previous Versions

See git history for earlier changes.
