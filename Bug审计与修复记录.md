# Bug 审计与修复记录

> 审计日期：2026-09-09　|　方式：四路并行静态审查（后端核心/RBAC、后端 AI/知识库、后端业务模块、前端全局）+ 高危项人工二次代码确认 + pytest/浏览器端到端验证
>
> 结论：共识别 **38 项问题**，已修复 **37 项**（其中 1 项为部分修复），全部 P0/P1/P2 清零；回归 62/62 测试通过，前后端 0 lint 错误，浏览器实测 AI 问答链路（检索→记忆→引用→done 统计）正常。

---

## 一、P0 紧急（安全提权 / 核心链路损坏）

| # | 状态 | 问题 | 位置 | 影响 | 修复方案 |
| --- | --- | --- | --- | --- | --- |
| 1 | ✅ | **角色提权**：`update_role` 可把任意角色改成 `role_type=1`（超管），`_set_user_roles` 可绑定超管角色，`delete/toggle/reset` 可操作超管账号——普通管理员可完全接管系统 | `role_service.py` `create/update_role`、`user_service.py` `_set_user_roles`/`update_user`/`delete_user`/`toggle_status`/`reset_password` | 持 `role:update`/`user:update` 者可自升超管、锁定/删除/重置真正超管 | 角色类型白名单 (1,2,3)；非超管禁设/禁绑 `role_type=1`；超管角色仅超管可编辑/授权且类型/状态不可改；`_ensure_can_manage_user` 保护绑定超管角色的账号 |
| 2 | ✅ | **权限不回收**：角色停用/软删后其绑定用户仍持有全部菜单与权限 | `menu_service.py` `_authorized_menu_ids`/`is_super_admin`/`collect_permissions` | UI 已删/停的角色权限照旧生效，权限管理形同虚设 | 三处查询补 `SysRole.status==1`；关联权限路径补 `SysMenu.status==1` |
| 3 | ✅ | **generate 节点必然崩溃**：同步 `for` 遍历 `achat_stream` 异步生成器 → TypeError | `ai_chat_service.py` `_generate_node._consume_stream` | 达到工具轮次上限/模型空回时整轮问答失败 | 改 `async def` + `async for`，调用处 `await` |
| 4 | ✅ | **RAG 答非所据**：检索正文从未进入模型可见消息，agent 携带正文走 direct 直接编答案，引用失实 | `ai_chat_service.py` `_tool_retrieve`/`_route_after_agent`/`_direct_node` | 知识库问答实为"无文档自答+假引用" | 路由修复：发生过工具调用强制走 `generate`（检索正文经 `_generate_system` 注入） |
| 5 | ✅ | **邀请可绑超管角色**：create 零校验、公开 accept 直接入绑定、token 明文返给列表查看者 | `invitation_service.py` `create_invitation`/`accept_invitation`/`serialize_invitation` | 持邀请权限者可批量制造超管账号 | 创建/accept 双重校验（存在/启用/非超管）；`FOR UPDATE` 行锁防并发注册；列表 token/链接按状态脱敏（仅 0/1/2 可见） |

## 二、P1 高（数据一致性 / 功能失效 / 安全弱项）

| # | 状态 | 问题 | 位置 | 影响 | 修复方案 |
| --- | --- | --- | --- | --- | --- |
| 6 | ✅ | 已确认/已发放月份仍可录奖惩，重算跳过 → 总额与明细永久不一致（金额静默丢失） | `salary_service.py` `create_adjustment` | 员工少发/漏扣无感知 | 录入前校验该月 payroll 状态，非草稿 422 拒绝；月份为空默认当前月（顺带修复 P3-31） |
| 7 | ✅ | 「加载更早消息」闭包捕获旧 `currentConvId`、游标不前进 → 失效/串会话/重复追加 | `useChat.ts` `loadOlderMessages`（220–249 行） | >100 条会话分页失效、消息重复堆叠 | 用参数 `id` 作会话 id；`oldestServerIdRef` 游标每次推进；`loadingOlderRef` 防并发；新建会话重置游标 |
| 8 | ✅ | NL2SQL 列表行「通过/驳回」审核的是流程区 `current`（错位/点击无效） | `views/ai/nl2sql/index.tsx` `openReview`/`handleReviewOk` | 误审放行危险 SQL | 新增 `reviewTarget` state，确定时以行记录为准 |
| 9 | ✅ | `JWT_SECRET` 默认公开值 `"change-me"` 可伪造任意用户令牌 | `core/config.py` | 完全绕过认证 | `get_settings` 检测默认/弱密钥 → 自动换进程级随机密钥 + ERROR 告警（生产请在 .env 配强密钥） |
| 10 | ✅ | `_client_ip` 无条件信任 `X-Forwarded-For`，可绕过 IP 限流/构造他人 IP 锁定 | `routers/auth.py` | 限流体系失效 | 新增 `TRUST_XFF` 配置（默认 False），仅可信反代部署时读取 |
| 11 | ✅ | async 节点内同步执行 RAG/NL2SQL（多轮网络 I/O）阻塞整个事件循环 | `ai_chat_service.py` `_tools_node` | 单次工具调用卡死全进程数秒~数十秒 | retrieve/nl2sql 放 `asyncio.to_thread` |
| 12 | ✅ | kb 会话删除后仍可列表/查看/续聊（无 `status!=2` 过滤） | `kb_chat_service.py` 三处 | 软删失效、AI 助手已删会话串入 kb 列表 | 列表/详情/续聊统一补过滤 |
| 13 | ✅ | 找回密码 IP 计数锁到期不清零 → 准永久限流（公开接口 DoS） | `password_recovery_service.py` `_check_ip_limit` | NAT 出口 IP 长期禁发验证码 | 锁到期清零重新计数 |
| 14 | ✅ | 审批驳回后停用账号残留 → 用户名永久占用（申请死锁） | `approval_service.py` `_finish_review`/`register_apply` | 被驳回者无法重申请且无接口释放 | 驳回时账号软删+改名释放用户名（审批表外键不可物理删）；占用检查排除 `status==2` |

## 三、P2 中

| # | 状态 | 问题 | 位置 | 修复方案 |
| --- | --- | --- | --- | --- |
| 15 | ✅ | 邀请/注册并发竞态（先查后插）；唯一约束冲突落 500 | `invitation_service`/`approval_service`/`main.py` | 邀请 accept 加 `FOR UPDATE` 行锁；全局 `IntegrityError` 处理器统一转 422 |
| 16 | ✅ | 记忆先提交 DB 再写向量：向量失败留下"永不召回"的行；同用户并发提取竞态 | `ai_memory_service.py` `save_memories` | flush 占位→向量成功→统一提交，失败回滚；按 user_id 进程内锁串行化 |
| 17 | ✅ | 文件重解析残留孤儿向量，旧版切片仍被检索命中 | `kb_rag_service.py` `process_file` | 清空旧切片行后同步 `remove_file_vectors` 清向量 |
| 18 | ✅ | 工具异常直接中断整轮 SSE，无降级 | `ai_chat_service.py` `_tools_node` | retrieve/nl2sql 单工具 try/except → 失败转结果文案，模型基于失败信息降级作答 |
| 19 | ✅ | `done.context_tokens` 用全轮 prompt 累加值当"单次占用"，多轮后误报超窗口 | `ai_chat_service.py` chat_sse 统计段 | 改取单次最大 prompt（`prompt_max`），分类明细按同口径缩放；计费口径 `usage` 不变 |
| 20 | ✅ | 生成中离开页面 SSE 不 abort：后台继续耗 token、卸载后 setState | `useChat.ts`、`ai/kb/chat/index.tsx` | 组件卸载 `useEffect` cleanup 调 `abortRef.current?.abort()` |
| 21 | ✅ | `downloadBlob` 绕过拦截器：401 不刷新、失败无提示、15s 超时、调用点无 catch | `api/request.ts` + org/user、attendance/record 调用点 | 401 手动刷新重放、统一错误 toast、120s 超时；调用点补 catch |
| 22 | ✅ | 找回密码返回"账号不存在"放大用户名枚举 | `password_recovery_service.py` `send_code` | 已配 SMTP（真实发信）时统一文案；演示回显模式保持明确报错 |
| 23 | ✅ | 登录 IP 失败锁到期不清零（与 #13 同类） | `auth_service.py` `login` | 锁到期清零计数 |
| 24 | ✅ | 部门/负责人外键零校验 → 500 或脏数据 | `user_service.py`、`department_service.py` | 新增 `_validate_department`/`_validate_leader`，create/update 全覆盖 |

## 四、P3 低（体验 / 健壮性）

| # | 状态 | 问题 | 位置 | 修复方案 |
| --- | --- | --- | --- | --- |
| 25 | ✅ | 注册/邀请密码规则仅 `min:6`，后端要求 ≥8 位含字母数字 | `login/index.tsx`、`invite/accept.tsx` | 前端 pattern 与 `validate_password` 对齐 |
| 26 | ✅ | 工资单确认/发放无二次确认，误触即锁账 | `salary/index.tsx` 操作列 | 包 `Popconfirm`（发放按钮 danger） |
| 27 | ✅ | 登录账号不存在时跳过 bcrypt，存在用户名枚举时间侧信道 | `auth_service.py` `login` | 不存在也执行哑哈希 verify 拉平耗时 |
| 28 | ✅ | `need_reset_pwd` 后端从不强制改密 | `auth_service.py` `_serialize_user`、`MainLayout.tsx`、`components/ForceChangePassword.tsx`（新增） | 临时密码可长期使用 | 登录返回携带标记（数据通道）+ 前端守卫：`need_reset_pwd=1` 时 MainLayout 内容区整体替换为强制改密页，改密成功清零标记后自动恢复；浏览器实测闭环（临时密码登录→拦截→改密→新密码直入工作台） |
| 29 | ✅ | `update_model` 可把停用模型设为默认 → 同类型无有效默认 | `ai_model_service.py` `update_model` | `is_default=1` 时校验目标状态必须启用 |
| 30 | ✅ | `MIMO_EMBEDDING_MODEL` 声明后从未被读取，embedding 兜底与配置不一致 | `ai_model_service.py` `resolve_embedding_config` | ZHIPU 优先不变，新增 `MIMO_API_KEY + MIMO_EMBEDDING_MODEL` 备选通道 |
| 31 | ✅ | 奖惩 `year_month` 可空 → 永不进任何工资单 | `salary_service.py` | 空月份默认归属当前月（随 #6 一并修复） |
| 32 | ✅ | 偏好 `exclude_none` 丢弃 null，无法清除已设项恢复默认 | `profile_service.py` `update_preferences` | 显式 null 语义为删除该键（`prefs.pop`） |
| 33 | ✅ | `initSession` 刷新令牌后 store 写回旧 token | `stores/user.ts` | 成功后重新 `getAccessToken()` 回写 |
| 34 | ✅ | 删除会话用闭包旧 `currentConvId` 判断，快速切换时误清空新会话 | `useChat.ts` `removeConversation` | 改用 `stateRef.current` 读最新值 |
| 35 | ⚠️ 部分修复 | 多数列表页 CRUD handler 无 catch（失败 unhandled rejection） | org/department、org/invitation 等 | 已补 department 删除、invitation 日志两处；**role/menu/user/position 等其余页面未逐一补**（拦截器已有统一 toast，属可选加固） |
| 36 | ✅ | `saveBlob` 立即 `revokeObjectURL`，个别浏览器下载中断 | `api/user.ts` | 延迟 1s 回收 |
| 37 | ✅ | 历史回放按"单条"截断，可能留下悬空 assistant 开头 | `ai_chat_service.py` `_build_history` | 改按整轮（user+assistant 一对）`del history[:2]` |
| 38 | ✅ | 图片 base64 按字符估 token 撑爆上下文口径；`file_read` 回显绝对路径 | `ai_chat_service.py`、`server_admin_service.py` | 新增 `_IMAGE_TOKEN_ESTIMATE=1000/张` 常量口径（`_estimate_question_tokens`/`_estimate_messages_tokens`）；文件结果仅回显文件名 |

---

## 五、修复涉及文件

**后端（12 个）**：`role_service.py`、`user_service.py`、`menu_service.py`、`invitation_service.py`、`ai_chat_service.py`、`ai_memory_service.py`、`kb_chat_service.py`、`kb_rag_service.py`、`salary_service.py`、`password_recovery_service.py`、`approval_service.py`、`auth_service.py`、`ai_model_service.py`、`profile_service.py`、`department_service.py`、`server_admin_service.py`、`core/config.py`、`routers/auth.py`、`main.py`

**前端（10 个）**：`api/request.ts`、`api/user.ts`、`stores/user.ts`、`views/ai/chat/useChat.ts`、`views/ai/kb/chat/index.tsx`、`views/ai/nl2sql/index.tsx`、`views/org/user/index.tsx`、`views/org/department/index.tsx`、`views/org/invitation/index.tsx`、`views/attendance/record/index.tsx`、`views/salary/index.tsx`、`views/login/index.tsx`、`views/invite/accept.tsx`

## 六、验证结果

- `pytest tests/` **62/62 通过**；`import app.main` 正常；前后端 **0 lint 错误**
- 后端已重启生效（进程无 `--reload`，后续改动需手动重启）
- 浏览器端到端冒烟：登录 → AI 助手提问「公司的年假制度是什么？」→ 检索工具执行 → 记忆召回 5 条 → **回答引用知识库真实正文**（年假 5/10/15 天、OA 审批等细节来自文档而非模型编造）→ 引用 `[1]` 编号有效 → `done` 统计正常产出（耗时 41.0s）——P0-3/P0-4 修复链路级验证通过

## 七、遗留事项（后续可选）

1. **P3-35 其余页面**：role/menu/user/position 等列表页 CRUD handler 补 catch（拦截器已有统一 toast，优先级低）
2. **生产部署**：`.env` 配置强随机 `JWT_SECRET`（未配置时系统会自动降级为随机密钥并在日志 ERROR 告警）；部署于反代之后时配置 `TRUST_XFF=true`
3. **可选增强**：邀请 accept 的行锁仅在 MySQL 生效；若未来迁移到无行锁的存储需改用条件 UPDATE 原子占用

## 八、补充修复记录（2026-09-09 追加）

- **P3-28 完整闭环**（原部分修复 → 已修复）：新增 `frontend/src/components/ForceChangePassword.tsx`；`MainLayout.tsx` 在 `need_reset_pwd===1` 时以改密页整体替换内容区（顶栏退出仍可用）；`types/index.ts` 的 `UserInfo` 补 `need_reset_pwd` 字段。端到端实测：管理员重置密码 → 临时密码登录被拦截（业务内容不可见）→ 提交改密后标记清零（API 确认）→ 新密码登录直达工作台不再拦截。测试账号已清理。

---

## 九、Agent 链路工程评估与 P0 加固（2026-09-13）

> 审计日期：2026-09-13　|　方式：Agent 全链路人工代码审查（编排/LLM 客户端/三工具/记忆/检索/连接池/测试覆盖六维评估：架构、代码质量、性能、可维护性、错误处理、可扩展性）
>
> 结论：产出完整评估报告与分级改进计划（P0 稳定性 4 项 / P1 结构治理 7 项 / P2 长期演进 6 项）。本轮实施 P0 全部 4 项，回归 144/144 测试通过（原 140 + 新增心跳单测 4），前端零改动。

| # | 优先级 | 问题 | 位置 | 影响 | 修复方案 |
| --- | --- | --- | --- | --- | --- |
| P0-1 | 🔴 | **chat_sse 全程持有 DB 会话**：一条流式问答（含多轮 LLM 调用，可达数分钟）独占一条连接，默认连接池仅 5+10，约 15 人并发提问即耗尽，拖垮登录/CRUD 等无关接口 | `ai_chat_service.py` `chat_sse`、`db/session.py` | 并发流式下全站接口被拖垮（容量风险） | 重构为两段短事务（会话准备 / 结果持久化），图执行与 LLM 流式期间不持有连接；`conv` 引用改为捕获的 `conv_id`/`message_id` 防分离实例访问；连接池显式 `pool_size=10, max_overflow=20` 并注释依据 |
| P0-2 | 🔴 | **流式重试重复输出**：`_generate_once` 降级重试不检查"已产出增量"，流中途失败（`achat_stream` 在 produced=True 时抛 HTTPException）会重发全文，用户看到内容重复 | `ai_chat_service.py` `_generate_node` | 用户可见缺陷（低概率、直接影响） | 新增 `emitted` 标记：任何正文/思考增量下发后，预算降级重试、剥图重试、空回答重试三条路径一律改为走 error 事件；原有合法重试路径（请求期被拒、无字节产出）行为不变 |
| P0-3 | 🟠 | **工具失败零日志**：`_tools_node` 捕获异常只拼降级文案不记日志；`server_admin` 异常静默；`vector_store.delete_ids` 吞掉一切异常 | `ai_chat_service.py` `_tools_node`、`server_admin_service.py`、`vector_store.py` | 生产工具静默失败无痕迹可查，排障靠猜 | 业务拒绝记 warning、未知异常记 `logger.exception`、未注册工具调用记 warning、沙箱路径越界拦截记 warning（安全审计留痕）、向量删除失败记 warning |
| P0-4 | 🟠 | **SSE 无心跳**：工具执行/LLM 决策阶段可数十秒无字节下发，nginx 等反代默认 60s 空闲超时掐断连接（开发直连不暴露，上代理即炸） | 新增 `utils/sse.py`、`routers/ai.py` | 反向代理部署下长问答随机断流 | 新增 `with_heartbeat` 包裹器：`asyncio.wait` 事件与心跳竞争，静默超 15s 插入 `": ping"` 注释行（SSE 标准忽略行，前端 `parseSSEBlock` 对无 data 行的块返回 null，零协议改动）；收尾覆盖两种时机——等待期被取消时把取消传播进源生成器（LLM 真正中断），yield 边界关闭时显式 aclose 源生成器（确定性触发 with 块清理） |

**修复涉及文件**：`app/services/ai_chat_service.py`（chat_sse 分段短事务 + emitted 守卫 + 工具日志）、`app/db/session.py`（连接池）、`app/services/server_admin_service.py` / `app/services/vector_store.py`（日志）、`app/utils/sse.py`（新增，心跳包裹器）、`app/routers/ai.py`（套用心跳）、`tests/test_sse_heartbeat.py`（新增，4 用例）

**验证结果**：pytest 144/144 通过（test_sse_heartbeat 4 用例：事件透传、静默期心跳插入、等待期取消传播、yield 边界关闭清理）；`app.main` 导入冒烟通过（27 路由）；前端无需改动。

**遗留（未在本轮范围）**：知识库问答端点（`routers/kb.py`）有相同反代超时暴露，但其 `chat_sse` 为同步生成器，套异步心跳需线程池桥接层，留待该服务异步化时一并覆盖；P1/P2 改进项（工具注册表化、llm_client 去重、用量配额、图片外置、BM25 性能等）见评估报告改进计划。
