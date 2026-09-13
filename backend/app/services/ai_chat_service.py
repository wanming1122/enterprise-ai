"""AI助手服务（M4-T3）：LangGraph Agent 问答，SSE 流式输出，会话与消息持久化。

图结构（设计方案 11.7）：agent 节点做工具调用决策 ⇄ tools 节点（retrieve 知识库检索、
nl2sql 产品数据查询）循环，决策完成后进入 generate 节点以 SSE 流式输出并附引用。
LLM 调用统一走 llm_client（httpx 直连 OpenAI 兼容接口）：推理型模型回放 tool_calls
必须携带 reasoning_content，由 chat_with_tools 返回的 dict 原样回放满足。
事件协议与 kb_chat_service 对齐：meta/tool/reasoning/message/citations/done/error。

M10：全链路异步化（async 节点 + astream + achat_*），客户端断连可真正取消 LLM 调用；
历史回放带工具结果摘要与 token 预算；引用编号越界校验；kb_ids 检索范围与 source 会话来源。
"""
import asyncio
import json
import logging
import operator
import re
import time
from datetime import datetime, timedelta
from typing import Annotated, Literal, TypedDict

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from langgraph.config import get_stream_writer
from langgraph.graph import END, StateGraph

from app.models.ai import AIConversation, AIMessage
from app.core.config import settings
from app.models.kb import KBKnowledgeBase
from app.models.nl2sql import NL2SQLRecord
from app.services import ai_memory_service, ai_model_service, kb_rag_service, llm_client, nl2sql_service, server_admin_service
from app.services.operation_log_service import write_log
from app.db.session import SessionLocal

logger = logging.getLogger(__name__)

HISTORY_ROUNDS = 4          # 多轮上下文回放最近 4 轮 user/assistant 终答
MAX_TOOL_ROUNDS = 6         # 单次提问工具调用轮次上限
TOOL_RESULT_MAX_CHARS = 4000
TOOL_STORE_MAX_CHARS = 2000  # tool 消息持久化截断长度
HISTORY_TOKEN_BUDGET = 6000  # 历史回放 token 预算：超出从最旧轮截断
TOOL_SUMMARY_CHARS = 300     # 历史回放中 tool 结果摘要长度

# 多模态图片约束（Data URL 直存方案，与头像一致；SSE 请求体携带）
_IMAGE_RE = re.compile(r"^data:image/(png|jpe?g|webp);base64,[A-Za-z0-9+/=\s]+$")
MAX_IMAGE_CHARS = 4_000_000  # 单张 Data URL 字符上限（约 3MB 原图）


def validate_images(images: list[str]) -> list[str]:
    """校验随问图片：仅 png/jpeg/webp Data URL、单张 ≤4M 字符、最多 3 张。SSE 开始前调用。"""
    if not images:
        return []
    if len(images) > 3:
        raise HTTPException(status_code=422, detail="每次最多附带 3 张图片")
    for img in images:
        if not _IMAGE_RE.match(img or ""):
            raise HTTPException(status_code=422, detail="仅支持 png/jpg/webp 图片")
        if len(img) > MAX_IMAGE_CHARS:
            raise HTTPException(status_code=422, detail="单张图片过大（压缩后需小于 3MB）")
    return images


def _user_content(question: str, images: list[str]) -> str | list[dict]:
    """当前轮 user 消息体：带图时用 OpenAI 多模态 content parts，否则纯文本。"""
    if not images:
        return question
    return [
        {"type": "text", "text": question},
        *[{"type": "image_url", "image_url": {"url": img}} for img in images],
    ]


def _strip_images(messages: list[dict]) -> list[dict]:
    """把消息中的多模态 content parts 退化为纯文本（模型不支持视觉时降级用）。"""
    out: list[dict] = []
    for m in messages:
        if isinstance(m.get("content"), list):
            text = " ".join(p.get("text", "") for p in m["content"] if p.get("type") == "text")
            out = [*out, {**m, "content": f"{text}\n（用户上传了图片，当前模型不支持图片识别，请据文字回答）"}]
        else:
            out = [*out, m]
    return out

# ---------- 工具定义与提示词 ----------

TOOLS_SPEC = [
    {
        "type": "function",
        "function": {
            "name": "retrieve",
            "description": "检索企业知识库文档片段。回答公司制度、流程、文档内容等问题前必须先调用。",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "检索关键词或问题改写"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "server_admin",
            "description": "只读探查服务器状态（系统信息/磁盘/进程/网络/项目文件），全部为只读操作。仅当用户询问服务器、磁盘、进程、网络或项目文件相关问题时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["system_info", "disk", "process", "network", "file_list", "file_read"],
                        "description": "要执行的只读探查动作",
                    },
                    "path": {"type": "string", "description": "file_list/file_read 的项目内相对路径，缺省为项目根目录"},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "nl2sql",
            "description": (
                "查询业务数据库的实时数据，支持四张表：product(产品)、sys_user(员工)、"
                "att_record(考勤记录)、sal_payroll(工资单)。"
                "回答产品库存价格、员工信息、考勤记录、薪资统计等结构化数据问题前必须先调用。"
                "支持跨表JOIN查询。"
            ),
            "parameters": {
                "type": "object",
                "properties": {"question": {"type": "string", "description": "自然语言数据问题"}},
                "required": ["question"],
            },
        },
    },
]

AGENT_SYSTEM_PROMPT = (
    "你是企业管理系统的AI助手，负责回答员工关于公司制度流程与业务数据的问题。"
    "需要查资料或查数据时调用相应工具（各工具用途与触发条件见工具定义）。\n"
    "规则：每次只调用一个工具；工具结果足够时不要再调用；与制度、业务数据无关的问题直接回答，不调用工具。"
)

GENERATE_SYSTEM_PROMPT = (
    "你是企业管理系统的智能助手，用简体中文回答。规则：\n"
    "1. 若提供了知识库参考资料，回答须依据资料，并在对应句子末尾用 [1][2] 标注引用，资料中没有的内容如实说明；\n"
    "2. 若对话中包含产品数据查询结果，用清晰的表格或列表呈现数据，禁止编造数字；\n"
    "3. 回答简洁、结构化，适合企业内部沟通场景。"
)


def _generate_system(context: str) -> str:
    if not context:
        return GENERATE_SYSTEM_PROMPT
    return f"{GENERATE_SYSTEM_PROMPT}\n\n参考资料：\n{context}"


# ---------- LangGraph 状态与节点 ----------


class AgentState(TypedDict, total=False):
    messages: Annotated[list[dict], operator.add]   # OpenAI 格式消息（含本轮 tool 消息）
    context_chunks: list[dict]                       # 最近一次 retrieve 命中的切片
    citations: list[dict]                            # generate 产出的引用列表
    tool_trace: Annotated[list[dict], operator.add]  # 工具调用记录
    usage_log: Annotated[list[dict], operator.add]   # 各次 LLM 调用的 token 用量（estimated 标记是否估算）
    rounds: int                                      # 已发生工具调用轮次
    user_id: int
    model_id: int | None                             # 用户指定的生成模型配置ID（空则默认模型）
    kb_ids: list[int] | None                         # 知识库检索范围（kb 问答调试页限定；空则全库）
    username: str
    deep_thinking: bool
    images: list[str]
    server_admin_enabled: bool
    answer: str
    reasoning: str


_IMAGE_TOKEN_ESTIMATE = 1000  # 单张图片的视觉 token 估算（base64 字符数与视觉计费无对应关系）


def _estimate_question_tokens(question: str, images: list[str]) -> int:
    """当前提问的 token 估算：图片按固定常量计，Data URL 文本不参与估算。"""
    return ai_memory_service.estimate_tokens(question) + len(images) * _IMAGE_TOKEN_ESTIMATE


def _estimate_messages_tokens(messages: list[dict]) -> int:
    """消息列表的启发式 token 估算；多模态图片按固定常量计（base64 字符数换算会严重失真）。"""
    total = 0
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):
            imgs = 0
            texts: list[str] = []
            for p in content:
                if isinstance(p, dict):
                    if p.get("type") == "image_url":
                        imgs += 1
                    else:
                        texts.append(str(p.get("text") or ""))
            total += imgs * _IMAGE_TOKEN_ESTIMATE
            total += ai_memory_service.estimate_tokens(" ".join(texts))
        else:
            total += ai_memory_service.estimate_tokens(str(content or ""))
    return total


async def _agent_node(state: AgentState) -> dict:
    """工具调用决策：达到轮次上限则不再调用工具，直接进入生成。"""
    if state.get("rounds", 0) >= MAX_TOOL_ROUNDS:
        return {"messages": [{"role": "assistant", "content": ""}]}
    usage_out: dict = {}
    mid = state.get("model_id")
    deep = bool(state.get("deep_thinking"))
    # 非深度思考时关闭上游思考（智谱等）：把预算留给工具决策，避免"只思考不产出"空回；
    # 开启深度思考时思考与工具调用共用更大配额
    agent_tokens = 4096 if deep else 1536
    try:
        msg = await llm_client.achat_with_tools(
            state["messages"], TOOLS_SPEC, max_tokens=agent_tokens,
            usage_out=usage_out, model_id=mid, thinking=deep,
        )
    except HTTPException:
        if not state.get("images"):
            raise
        # 带图提问且模型不支持视觉输入时，退化纯文本重试一次
        msg = await llm_client.achat_with_tools(
            _strip_images(state["messages"]), TOOLS_SPEC, max_tokens=agent_tokens,
            usage_out=usage_out, model_id=mid, thinking=deep,
        )
    if usage_out.get("total_tokens") is not None:
        usage = {
            "prompt_tokens": int(usage_out.get("prompt_tokens") or 0),
            "completion_tokens": int(usage_out.get("completion_tokens") or 0),
            "cached_tokens": int(
                ((usage_out.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0
            ),
            "estimated": False,
        }
    else:  # 上游未回传 usage → 启发式估算
        usage = {
            "prompt_tokens": _estimate_messages_tokens(state["messages"]),
            "completion_tokens": ai_memory_service.estimate_tokens(json.dumps(msg, ensure_ascii=False)),
            "estimated": True,
        }
    return {"messages": [msg], "usage_log": [usage]}


def _route_after_agent(state: AgentState) -> Literal["tools", "generate", "direct"]:
    last = state["messages"][-1]
    if last.get("tool_calls"):
        return "tools"
    if (last.get("content") or "").strip():
        # agent 已产出终答：知识库检索的正文在 context_chunks（agent 不可见），必须走
        # generate 注入后再作答，否则只见过工具摘要行会编造答案、引用与正文不对应；
        # nl2sql/server_admin 的结果已在 tool 消息内，agent 的作答即最终答案，直接下发
        # （同时避免再向以 assistant 结尾的请求要答案——GLM 会因此返回空正文）
        return "generate" if state.get("context_chunks") else "direct"
    return "generate"


async def _tools_node(state: AgentState) -> dict:
    """执行本轮全部工具调用，tool 结果消息进入 messages，供 agent 复盘与 generate 引用。"""
    writer = get_stream_writer()
    db = SessionLocal()
    tool_msgs: list[dict] = []
    trace: list[dict] = []
    try:
        last = state["messages"][-1]
        for tc in last.get("tool_calls", []):
            name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}
            if name == "retrieve":
                query = str(args.get("query") or "").strip()
                writer({"kind": "tool", "tool": "retrieve", "query": query})
                # RAG 检索含 Chroma/Embedding/LLM 网络调用：放线程池避免阻塞事件循环；
                # 单工具失败降级为结果文案，不中断整轮问答
                try:
                    content, chunks = await asyncio.to_thread(
                        _tool_retrieve, db, query, state.get("kb_ids")
                    )
                except HTTPException as exc:
                    logger.warning("retrieve 工具失败（业务拒绝）query=%r: %s", query, exc.detail)
                    content = f"知识库检索暂不可用：{exc.detail}。请基于已有知识回答，并如实告知用户检索服务暂时不可用。"
                    chunks = []
                except Exception as exc:  # noqa: BLE001
                    logger.exception("retrieve 工具执行异常 query=%r", query)
                    content = f"知识库检索执行异常：{exc}。请基于已有知识回答，并如实告知用户检索服务暂时不可用。"
                    chunks = []
                if chunks:
                    result = {"context_chunks": chunks}
                else:
                    result = {}
            elif name == "server_admin":
                action = str(args.get("action") or "")
                path = str(args.get("path") or "") or None
                if not state.get("server_admin_enabled"):
                    content = "当前账号没有服务器管理权限，无法执行该操作。请告知用户联系管理员开通。"
                else:
                    writer({"kind": "tool", "tool": "server_admin", "action": action, "path": path})
                    # subprocess 最多 15s：放入线程池，避免阻塞事件循环
                    content = await asyncio.to_thread(
                        server_admin_service.run_action, action, {"path": path}
                    )
                result = {}
            elif name == "nl2sql":
                question = str(args.get("question") or "").strip()
                writer({"kind": "tool", "tool": "nl2sql", "question": question})
                # SQL 生成 + 只读执行为同步网络 I/O：放线程池避免阻塞事件循环；
                # 失败降级为结果文案，不中断整轮问答
                try:
                    content = await asyncio.to_thread(_tool_nl2sql, db, state, question)
                except HTTPException as exc:
                    logger.warning("nl2sql 工具失败（业务拒绝）question=%r: %s", question, exc.detail)
                    content = f"数据查询暂不可用：{exc.detail}。请如实告知用户当前无法查询产品数据。"
                except Exception as exc:  # noqa: BLE001
                    logger.exception("nl2sql 工具执行异常 question=%r", question)
                    content = f"数据查询执行异常：{exc}。请如实告知用户当前无法查询产品数据。"
                result = {}
            else:
                logger.warning("模型调用了未注册工具 %r，已拒绝", name)
                content = f"未知工具：{name}"
                result = {}
            tool_msgs.append({"role": "tool", "tool_call_id": tc.get("id"), "name": name, "content": content})
            trace.append({"tool": name, "args": args})
        return {"messages": tool_msgs, "tool_trace": trace, "rounds": state.get("rounds", 0) + 1, **result}
    finally:
        db.close()


def _tool_retrieve(db: Session, query: str, kb_ids: list[int] | None = None) -> tuple[str, list[dict]]:
    """知识库检索：返回 (tool结果文本, 命中切片)。kb_ids 非空时限定检索范围（kb 问答调试）。"""
    q = select(KBKnowledgeBase.id).where(KBKnowledgeBase.status == 1)
    if kb_ids:
        q = q.where(KBKnowledgeBase.id.in_(kb_ids))
    kb_ids = list(db.scalars(q).all())
    if not kb_ids:
        return "当前没有启用的知识库，无法检索。请基于已有知识回答，并说明未检索到资料。", []
    if not query:
        return "检索词为空，请提供具体问题。", []
    results, _ = kb_rag_service.retrieve_with_crag(db, query=query, kb_ids=kb_ids, top_k=6)
    if not results:
        return "知识库中未检索到相关资料。请如实告知用户未找到依据。", []
    return f"已检索到 {len(results)} 条相关资料（文件/标题/页码/正文），已注入生成上下文。", results


def _tool_nl2sql(db: Session, state: AgentState, question: str) -> str:
    """产品数据查询：生成 SQL（含安全校验）→ 只读执行 → 落 nl2sql_record 留痕。"""
    if not question:
        return "数据问题为空，请提供具体问题。"
    sql = nl2sql_service.generate_sql(question)  # 内含 SELECT/仅product/LIMIT 校验，不合法 422
    rows, ms = nl2sql_service.execute_readonly(sql)
    payload = {"columns": list(rows[0].keys()) if rows else [], "rows": rows}
    payload = json.loads(json.dumps(payload, ensure_ascii=False, default=str))
    rec = NL2SQLRecord(
        user_id=state["user_id"], question=question, generated_sql=sql,
        review_status=3, result_json=json.dumps(payload, ensure_ascii=False),
        execution_ms=ms, executed_at=datetime.now(),
    )
    db.add(rec)
    db.commit()
    write_log(db, user_id=state["user_id"], username=state.get("username"), module="NL2SQL",
              action="AI助手执行SQL", params={"question": question, "rows": len(rows), "ms": ms}, result=1)
    preview = json.dumps(rows[:20], ensure_ascii=False, default=str)[:TOOL_RESULT_MAX_CHARS]
    result = (f"已执行只读SQL：{sql}\n共 {len(rows)} 行，耗时 {ms} ms。"
              f"结果JSON（最多前20行）：{preview}")
    if not rows:
        # 0 行自纠：模型可能自行添加了过严的过滤条件（例如把业务状态当软删除过滤），
        # 显式提示其去掉多余筛选后重查一次，借助 agent⇄tools 循环自我纠正
        result += (
            "\n注意：本次查询返回 0 行。若用户预期应有数据，可能是过滤条件过严"
            "（例如多余的 status 过滤），请去掉不必要的筛选条件后重新调用 nl2sql 查询一次，再据结果作答。"
        )
    return result


def _strip_invalid_citations(answer: str, citation_count: int) -> str:
    """引用编号校验：剔除超出引用范围的 [n] 标注（防止模型编造编号）。"""
    if citation_count <= 0:
        return re.sub(r"\[\d+\]", "", answer)

    def _repl(m: re.Match) -> str:
        return m.group(0) if 1 <= int(m.group(1)) <= citation_count else ""

    return re.sub(r"\[(\d+)\]", _repl, answer)


async def _generate_node(state: AgentState) -> dict:
    """最终回答：流式产出（custom 事件逐 delta 上报），引用随流下发。"""
    writer = get_stream_writer()
    chunks = state.get("context_chunks") or []
    context = "\n\n".join(
        f"[{i + 1}] （文件：{c.get('file_name')}，标题路径：{c.get('title_path')}，页码：{c.get('page')}）\n{c.get('content')}"
        for i, c in enumerate(chunks)
    )
    # 请求若以 assistant 结尾，部分模型（如 GLM-4-Flash）视为该轮已结束而返回空正文：
    # 剔除 agent 已产出的「纯文本终答」（保留 assistant.tool_calls 与 tool 结果的配对）。
    base = list(state["messages"])
    while len(base) > 1 and base[-1].get("role") == "assistant" and not base[-1].get("tool_calls"):
        base.pop()
    msgs = [{"role": "system", "content": _generate_system(context)}] + base
    deep = state.get("deep_thinking", False)
    answer: list[str] = []
    reasoning: list[str] = []
    usage_acc = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "has_real": False}
    # 是否已向客户端下发过任何增量（正文或思考）：为真后禁止静默重试，
    # 否则重发的完整内容会与已下发的前半段在用户界面上重复
    emitted = {"any": False}

    async def _consume_stream(stream) -> None:
        """消费流式输出：正文/思考增量下发，usage 累计（上游支持时为真实值）。

        stream 为异步生成器（achat_stream），必须用 async for 消费。
        """
        async for kind, delta in stream:
            if kind == "usage":
                usage_acc["prompt_tokens"] += int(delta.get("prompt_tokens") or 0)
                usage_acc["completion_tokens"] += int(delta.get("completion_tokens") or 0)
                usage_acc["cached_tokens"] += int(
                    ((delta.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0
                )
                usage_acc["has_real"] = True
            elif kind == "reasoning":
                if deep:
                    emitted["any"] = True
                    reasoning.append(delta)
                    writer({"kind": "reasoning", "delta": delta})
            else:
                emitted["any"] = True
                answer.append(delta)
                writer({"kind": "message", "delta": delta})

    mid = state.get("model_id")

    async def _consume_once(stream_msgs: list[dict], max_tokens: int) -> None:
        """清空累积后消费一次流：供首次调用/剥图重试/空回答重试复用，避免内容与用量重复。"""
        answer.clear()
        reasoning.clear()
        usage_acc.update({"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "has_real": False})
        await _consume_stream(
            llm_client.achat_stream(stream_msgs, max_tokens=max_tokens, model_id=mid, thinking=deep)
        )

    # 输出预算：非深度思考时上游思考已关闭，沿用原预算即可；深度思考时给足思考+正文空间。
    # 若上游因超出该模型输出上限而拒绝（4xx），自动降级到保守预算重试，避免"调大反而失败"。
    first_tokens = 8192 if deep else 3072
    retry_tokens = 16384 if deep else 4096
    safe_tokens = 3072

    async def _generate_once(stream_msgs: list[dict], budget: int) -> None:
        """按 budget 生成；仅当尚未下发任何增量且上游因超出输出上限拒绝时，
        降级到保守预算再试一次（该拒绝发生在请求期、无字节产出，重试不会重复）。
        已下发增量（emitted）后一律不重试：流中途失败重发会使用户看到重复内容。
        """
        try:
            await _consume_once(stream_msgs, budget)
        except HTTPException:
            if budget <= safe_tokens or emitted["any"]:
                raise
            await _consume_once(stream_msgs, safe_tokens)

    effective_msgs = msgs
    try:
        try:
            await _generate_once(msgs, first_tokens)
        except HTTPException:
            if not state.get("images") or emitted["any"]:
                raise
            # 带图提问且模型不支持视觉输入（请求即被拒，尚未产出增量）时，退化纯文本重试
            effective_msgs = _strip_images(msgs)
            await _generate_once(effective_msgs, first_tokens)
    except HTTPException as exc:
        writer({"kind": "error", "message": str(exc.detail)})
        return {"answer": "", "reasoning": "", "citations": [], "error": str(exc.detail)}

    if not "".join(answer).strip() and not emitted["any"]:
        # 空回答兜底：推理型模型在长上下文（含工具结果）下可能只输出思考不输出正文，
        # 加大配额重试一次；上游拒绝更大预算时忽略，走下方空回答提示而非整轮失败。
        # 若思考增量已下发（emitted），重试会让思考面板重复，直接走空回答提示
        try:
            await _generate_once(effective_msgs, retry_tokens)
        except HTTPException:
            pass

    if not "".join(answer).strip():
        # 两次尝试均无正文：记录诊断信息（思考长度/用量），转错误事件让前端展示重试入口
        logger.warning(
            "AI 助手空回答：model_id=%s deep=%s 思考长度=%d 用量=%s",
            mid, deep, len("".join(reasoning)), usage_acc,
        )
        empty_msg = "本次未生成有效回答，请重试；若持续出现，可在右下角切换其他模型"
        writer({"kind": "error", "message": empty_msg})
        return {"answer": "", "reasoning": "", "citations": [], "error": empty_msg}
    if usage_acc["has_real"]:
        usage = {
            "prompt_tokens": usage_acc["prompt_tokens"],
            "completion_tokens": usage_acc["completion_tokens"],
            "cached_tokens": usage_acc["cached_tokens"],
            "estimated": False,
        }
    else:  # 上游未下发 usage → 启发式估算
        usage = {
            "prompt_tokens": _estimate_messages_tokens(effective_msgs),
            "completion_tokens": ai_memory_service.estimate_tokens("".join(answer)),
            "estimated": True,
        }
    citations = _build_citations(chunks)
    final_answer = _strip_invalid_citations("".join(answer), len(citations))
    if citations:
        writer({"kind": "citations", "citations": citations})
    return {"answer": final_answer, "reasoning": "".join(reasoning), "citations": citations, "usage_log": [usage]}


def _build_citations(chunks: list[dict]) -> list[dict]:
    return [
        {
            "index": i + 1,
            "file_name": c.get("file_name"),
            "title_path": c.get("title_path"),
            "page": c.get("page"),
            "similarity": c.get("similarity"),
            "snippet": (c.get("content") or "")[:120] + "…",
        }
        for i, c in enumerate(chunks)
    ]


def _direct_node(state: AgentState) -> dict:
    """Agent 已直接回答（无工具调用），直接下发其内容，不二次调用 LLM。"""
    writer = get_stream_writer()
    last = state["messages"][-1]
    citations = _build_citations(state.get("context_chunks") or [])
    content = _strip_invalid_citations(last.get("content") or "", len(citations))
    reasoning = last.get("reasoning_content") or ""
    # 深度思考：直接下发时也把 agent 的思考推给前端，保持与 generate 路径一致的展示
    if state.get("deep_thinking") and reasoning:
        writer({"kind": "reasoning", "delta": reasoning})
    if content:
        writer({"kind": "message", "delta": content})
    if citations:
        writer({"kind": "citations", "citations": citations})
    return {"answer": content, "reasoning": reasoning, "citations": citations}


def _build_graph():
    g = StateGraph(AgentState)
    g.add_node("agent", _agent_node)
    g.add_node("tools", _tools_node)
    g.add_node("generate", _generate_node)
    g.add_node("direct", _direct_node)
    g.set_entry_point("agent")
    g.add_conditional_edges("agent", _route_after_agent, {"tools": "tools", "generate": "generate", "direct": "direct"})
    g.add_edge("tools", "agent")
    g.add_edge("generate", END)
    g.add_edge("direct", END)
    return g.compile()


_GRAPH = _build_graph()


# ---------- SSE 问答 ----------


def _sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _build_history(db: Session, conversation_id: int) -> list[dict]:
    """构建历史回放：最近若干条消息，tool 结果以摘要前缀并入其后的 assistant 消息，
    并按 token 预算从最旧侧截断（与真实请求口径一致：仅最近 HISTORY_ROUNDS 轮）。"""
    recent = db.scalars(
        select(AIMessage)
        .where(AIMessage.conversation_id == conversation_id)
        .order_by(AIMessage.id.desc())
        .limit(HISTORY_ROUNDS * 4)
    ).all()
    recent.reverse()
    history: list[dict] = []
    pending_tools: list[str] = []
    for m in recent:
        if m.role == "user":
            history.append({"role": "user", "content": m.content or ""})
        elif m.role == "tool":
            pending_tools.append((m.content or "")[:TOOL_SUMMARY_CHARS])
        elif m.role == "assistant":
            content = m.content or ""
            if pending_tools:
                content = "[此前工具结果] " + " | ".join(pending_tools) + "\n" + content
                pending_tools = []
            history.append({"role": "assistant", "content": content})
    # token 预算：超出从最旧侧按整轮（user+assistant 一对）截断，
    # 逐条弹出可能留下「无前置 user 的悬空 assistant」，影响模型表现
    while len(history) > 2:
        used = sum(ai_memory_service.estimate_tokens(str(h["content"])) for h in history)
        if used <= HISTORY_TOKEN_BUDGET:
            break
        del history[:2]
    return history


async def chat_sse(
    user_id: int, username: str, *,
    question: str, conversation_id: int | None, deep_thinking: bool,
    images: list[str] | None = None, enable_server_admin: bool = False,
    result_holder: dict | None = None, model_id: int | None = None,
    kb_ids: list[int] | None = None, source: str = "ai",
    request=None,
):
    """SSE 生成器（异步）：建/续会话 → 运行 LangGraph 图并转发 custom 事件 → 持久化消息。

    result_holder 由路由层传入并转交 BackgroundTask：流结束后回填 ok/conversation_id，
    供异步记忆提取使用（客户端在 done 后断开也不影响后台提取）。
    request 用于断连检测：客户端中断后尽快取消 LLM 调用，避免 token 白白消耗。
    库会话分段持有：仅「会话准备」与「结果持久化」两个短事务占用连接，图执行与
    LLM 流式期间不占连接池——一条流式问答可持续数分钟，全程持有会耗尽连接池，
    拖垮登录、CRUD 等无关接口。
    """
    started = time.perf_counter()
    try:
        # 当前生效的生成模型配置：取真实上下文窗口与模型名（未配置窗口则回退全局常量；
        # resolve_llm_config 自带短会话，不占用本请求连接）
        try:
            llm_cfg = ai_model_service.resolve_llm_config(model_id)
            context_window = int(llm_cfg.get("context_window") or 0) or settings.CONTEXT_WINDOW_TOKENS
            model_name = str(llm_cfg.get("model_name") or "")
        except HTTPException:
            context_window = settings.CONTEXT_WINDOW_TOKENS
            model_name = ""

        # ---------- 短事务 1：会话准备（建/续会话 → 历史 → 记忆召回 → 落用户消息） ----------
        with SessionLocal() as db:
            if conversation_id:
                conv = db.get(AIConversation, conversation_id)
                if conv is None or conv.status == 2 or conv.user_id != user_id:
                    raise HTTPException(status_code=404, detail="会话不存在")
                conv_id = conv.id
            else:
                conv = AIConversation(user_id=user_id, title=question[:32] or "新会话", status=1, source=source)
                db.add(conv)
                db.commit()
                conv_id = conv.id
            yield _sse("meta", {"conversation_id": conv_id})
            if request is not None and await request.is_disconnected():
                return

            history = _build_history(db, conv_id)

            # 长期记忆召回：按当前问题检索本人记忆注入 system prompt（失败静默降级；
            # embedding 为网络调用，放入线程池避免阻塞事件循环）
            system_prompt = AGENT_SYSTEM_PROMPT
            memories: list[dict] = []
            if ai_memory_service.memory_enabled(db, user_id):
                try:
                    memories = await asyncio.to_thread(ai_memory_service.recall, db, user_id, question)
                except Exception:
                    memories = []
                injection = ai_memory_service.format_injection(memories)
                if injection:
                    system_prompt += injection
            if memories:
                yield _sse("memory", {
                    "count": len(memories),
                    "items": [{"id": m["id"], "type": m["memory_type"], "content": m["content"][:60]} for m in memories],
                })
            if request is not None and await request.is_disconnected():
                return

            imgs = validate_images(images or [])
            db.add(AIMessage(
                conversation_id=conv_id, role="user", content=question,
                attachments=[{"type": "image", "url": img} for img in imgs] or None,
            ))
            db.commit()
        # 会话已关闭：图执行与 LLM 流式期间不持有连接

        init_messages = [
            {"role": "system", "content": system_prompt},
            *history,
            {"role": "user", "content": _user_content(question, imgs)},
        ]
        # 上下文容量分类明细（启发式估算；工具结果在图执行后累计）
        injection_text = ai_memory_service.format_injection(memories)
        breakdown: list[dict] = [
            {"label": "系统提示词", "tokens": ai_memory_service.estimate_tokens(AGENT_SYSTEM_PROMPT)},
            {"label": "长期记忆", "tokens": ai_memory_service.estimate_tokens(injection)},
            {"label": "历史消息", "tokens": sum(ai_memory_service.estimate_tokens(str(m.get("content") or "")) for m in history)},
            {
                "label": "当前提问",
                "tokens": _estimate_question_tokens(question, imgs),
            },
            {"label": "工具定义", "tokens": ai_memory_service.estimate_tokens(json.dumps(TOOLS_SPEC, ensure_ascii=False))},
        ]
        init_state: AgentState = {
            "messages": init_messages,
            "context_chunks": [],
            "citations": [],
            "tool_trace": [],
            "usage_log": [],
            "rounds": 0,
            "user_id": user_id,
            "model_id": model_id,
            "kb_ids": kb_ids,
            "username": username,
            "deep_thinking": deep_thinking,
            "images": imgs,
            "server_admin_enabled": enable_server_admin,
        }
        final_state: AgentState = {}
        try:
            async for mode, payload in _GRAPH.astream(
                init_state, stream_mode=["custom", "values"], config={"recursion_limit": 50}
            ):
                if request is not None and await request.is_disconnected():
                    return  # 客户端断连：取消图执行，LLM 调用随之被真正中断
                if mode == "custom":
                    kind = payload.get("kind")
                    if kind == "reasoning":
                        yield _sse("reasoning", {"delta": payload["delta"]})
                    elif kind == "message":
                        yield _sse("message", {"conversation_id": conv_id, "delta": payload["delta"]})
                    elif kind == "tool":
                        yield _sse("tool", {k: v for k, v in payload.items() if k != "kind"})
                    elif kind == "citations":
                        yield _sse("citations", payload["citations"])
                    elif kind == "error":
                        yield _sse("error", {"message": payload["message"]})
                        return
                else:
                    final_state = payload
        except HTTPException as exc:
            yield _sse("error", {"message": str(exc.detail)})
            return
        except Exception as exc:  # 图执行异常兜底，避免连接悬挂
            yield _sse("error", {"message": f"处理失败：{exc}"})
            return

        # 用量统计：聚合本轮全部 LLM 调用（agent 决策 N 次 + generate 一次），随 assistant 消息落库供成本监控
        usage_log = final_state.get("usage_log") or []
        estimated = any(u.get("estimated") for u in usage_log)
        prompt_total = sum(int(u.get("prompt_tokens") or 0) for u in usage_log)
        completion_total = sum(int(u.get("completion_tokens") or 0) for u in usage_log)
        # 「上下文占用」取单次最大 prompt：agent 每轮决策都会重发完整历史，
        # prompt_total 是全轮累加的计费口径，直接当占用会高估（多轮后误报超窗口）
        prompt_max = max((int(u.get("prompt_tokens") or 0) for u in usage_log), default=0)
        # 缓存命中率（上游 prompt_tokens_details.cached_tokens 支持时才有值，否则为 None）
        cached_total = sum(int(u.get("cached_tokens") or 0) for u in usage_log)
        cache_hit_rate = (
            round(cached_total / prompt_total, 4) if prompt_total > 0 and cached_total > 0 else None
        )
        usage_stats = {
            "prompt_tokens": prompt_total,
            "completion_tokens": completion_total,
            "estimated": estimated,
            "model": model_name,
            # 以下两项非计费口径：供打开历史会话时还原「真实上下文占用」与缓存命中率
            "context_tokens": prompt_max,
            "cache_hit_rate": cache_hit_rate,
        }
        usage_stats["total_tokens"] = prompt_total + completion_total

        # ---------- 短事务 2：结果持久化 ----------
        # 先落 tool 消息再落 assistant 终答，保证时间线为：工具调用 → 最终回答
        with SessionLocal() as db:
            for m in final_state.get("messages", []):
                if m.get("role") == "tool":
                    db.add(AIMessage(
                        conversation_id=conv_id, role="tool", tool_name=m.get("name"),
                        content=(m.get("content") or "")[:TOOL_STORE_MAX_CHARS],
                    ))
            assistant = AIMessage(
                conversation_id=conv_id,
                role="assistant",
                content=final_state.get("answer", ""),
                reasoning_content=(final_state.get("reasoning") or None) if deep_thinking else None,
                citations=final_state.get("citations") or None,
                usage=usage_stats,
            )
            db.add(assistant)
            conv = db.get(AIConversation, conv_id)
            conv.updated_at = datetime.now()
            db.commit()
            message_id = assistant.id
        # 工具结果容量（本轮 tool 消息），并入分类明细与总容量
        tool_results = [
            m for m in final_state.get("messages", []) if m.get("role") == "tool"
        ]
        breakdown.append({
            "label": "工具结果",
            "tokens": sum(ai_memory_service.estimate_tokens(str(m.get("content") or "")) for m in tool_results),
        })
        estimated_total = sum(b["tokens"] for b in breakdown)
        # 真实值校准：上游回传 usage 时，用单次最大 prompt（prompt_max，前面已算）作已用量，
        # 并按比例缩放分类明细使合计与已用量一致（估算仅作兜底）
        if not estimated and prompt_max > 0 and estimated_total > 0:
            factor = prompt_max / estimated_total
            breakdown = [
            # 保留真实 0 值（如本轮无工具结果）：仅非零项做缩放兜底，避免 0 被显示成 1
            {**b, "tokens": max(1, round(b["tokens"] * factor)) if b["tokens"] > 0 else 0}
            for b in breakdown
        ]
            context_used = prompt_max
        else:
            context_used = estimated_total
        duration_ms = int((time.perf_counter() - started) * 1000)
        done_payload = {
            "conversation_id": conv_id,
            "message_id": message_id,
            "usage": usage_stats,
            "duration_ms": duration_ms,
            "context_tokens": context_used,
            "context_window": context_window,
            "context_breakdown": breakdown,
            "memory_count": len(memories),
        }
        if cache_hit_rate is not None:
            done_payload["cache_hit_rate"] = cache_hit_rate
        yield _sse("done", done_payload)
        if result_holder is not None:
            result_holder.update(ok=True, conversation_id=conv_id)
    except HTTPException as exc:
        yield _sse("error", {"message": str(exc.detail)})


# ---------- 会话管理 ----------


def serialize_conversation(c: AIConversation) -> dict:
    return {
        "id": c.id,
        "title": c.title,
        "pinned": c.pinned == 1,
        "created_at": c.created_at.isoformat(),
        "updated_at": c.updated_at.isoformat(),
    }


def serialize_message(m: AIMessage) -> dict:
    return {
        "id": m.id,
        "role": m.role,
        "content": m.content,
        "reasoning_content": m.reasoning_content,
        "tool_name": m.tool_name,
        "citations": m.citations,
        "attachments": m.attachments,
        "usage": m.usage,
        "created_at": m.created_at.isoformat(),
    }


def list_conversations(
    db: Session, *, user_id: int, page: int = 1, page_size: int = 20, source: str = "ai"
) -> tuple[list[dict], int]:
    q = select(AIConversation).where(
        AIConversation.user_id == user_id, AIConversation.status != 2, AIConversation.source == source
    )
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    # 置顶优先，其次按最近活跃（updated_at）倒序，供会话时间分组使用
    items = db.scalars(
        q.order_by(
            AIConversation.pinned.desc(), AIConversation.updated_at.desc(), AIConversation.id.desc()
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return [serialize_conversation(c) for c in items], total


def _get_own_conversation(db: Session, conversation_id: int, user_id: int) -> AIConversation:
    conv = db.get(AIConversation, conversation_id)
    if conv is None or conv.status == 2 or conv.user_id != user_id:
        raise HTTPException(status_code=404, detail="会话不存在")
    return conv


def _estimate_conversation_context(messages: list[AIMessage]) -> dict:
    """会话历史上下文（与 chat_sse 同口径）。

    优先使用最近一轮落库的真实占用（usage.context_tokens = 该轮单次最大 prompt_tokens，
    由 chat_sse 写入），缺失时（旧数据 / 上游未回传 usage）退回启发式估算；同时返回最近
    一轮的缓存命中率（上游支持时才有）。不含长期记忆注入——打开会话不做向量召回。
    """
    recent = [m for m in messages if m.role in ("user", "assistant")][-HISTORY_ROUNDS * 2 :]
    tool_results = [m for m in messages if m.role == "tool"]
    breakdown = [
        {"label": "系统提示词", "tokens": ai_memory_service.estimate_tokens(AGENT_SYSTEM_PROMPT)},
        {
            "label": "工具定义",
            "tokens": ai_memory_service.estimate_tokens(json.dumps(TOOLS_SPEC, ensure_ascii=False)),
        },
        {"label": "历史消息", "tokens": sum(ai_memory_service.estimate_tokens(m.content or "") for m in recent)},
        {
            "label": "工具结果",
            "tokens": sum(ai_memory_service.estimate_tokens(m.content or "") for m in tool_results),
        },
    ]
    estimated_total = sum(b["tokens"] for b in breakdown)
    # 最近一轮 assistant 的真实占用与缓存命中率（倒序取第一条有值的）
    real_used = 0
    cache_hit_rate: float | None = None
    for m in reversed(messages):
        if m.role != "assistant" or not m.usage:
            continue
        used = int(m.usage.get("context_tokens") or 0)
        rate = m.usage.get("cache_hit_rate")
        if used > 0 or rate is not None:
            real_used = used
            cache_hit_rate = float(rate) if rate is not None else None
            break
    if real_used > 0 and estimated_total > 0:
        # 用真实总量按比例缩放分项（分项占比仍为估算，但合计与真实占用一致）
        factor = real_used / estimated_total
        breakdown = [
            # 保留真实 0 值（如本轮无工具结果）：仅非零项做缩放兜底，避免 0 被显示成 1
            {**b, "tokens": max(1, round(b["tokens"] * factor)) if b["tokens"] > 0 else 0}
            for b in breakdown
        ]
        total = real_used
    else:
        total = estimated_total
    return {"total": total, "breakdown": breakdown, "cache_hit_rate": cache_hit_rate}


MESSAGE_PAGE_SIZE = 100  # 会话消息分页大小（倒序取最近 N 条，前端可上滑加载更早）


def get_conversation(db: Session, conversation_id: int, operator: object) -> dict:
    """会话详情：最近 MESSAGE_PAGE_SIZE 条消息 + has_more（更早消息走 messages 接口分页取）。"""
    conv = _get_own_conversation(db, conversation_id, operator.id)
    total = db.scalar(
        select(func.count()).select_from(AIMessage).where(AIMessage.conversation_id == conv.id)
    ) or 0
    latest = db.scalars(
        select(AIMessage)
        .where(AIMessage.conversation_id == conv.id)
        .order_by(AIMessage.id.desc())
        .limit(MESSAGE_PAGE_SIZE)
    ).all()
    latest.reverse()
    return {
        **serialize_conversation(conv),
        "messages": [serialize_message(m) for m in latest],
        "message_total": total,
        "has_more": total > MESSAGE_PAGE_SIZE,
        # 上下文容量估算（与下一轮真实请求同口径：最近4轮 + 固定开销，不含记忆注入）
        "context_usage": _estimate_conversation_context(latest),
    }


def list_messages_before(
    db: Session, conversation_id: int, operator: object, *, before_id: int, limit: int = MESSAGE_PAGE_SIZE
) -> dict:
    """加载指定消息 ID 之前的更早消息（倒序取 limit 条后正序返回）。"""
    conv = _get_own_conversation(db, conversation_id, operator.id)
    older = db.scalars(
        select(AIMessage)
        .where(AIMessage.conversation_id == conv.id, AIMessage.id < before_id)
        .order_by(AIMessage.id.desc())
        .limit(limit)
    ).all()
    older.reverse()
    has_more = bool(older) and older[0].id > (
        db.scalar(select(AIMessage.id).where(AIMessage.conversation_id == conv.id).order_by(AIMessage.id.asc()).limit(1))
        or 0
    )
    return {"messages": [serialize_message(m) for m in older], "has_more": has_more}


def usage_summary(db: Session, user_id: int, *, days: int = 30) -> dict:
    """用量汇总（成本监控）：聚合最近 N 天当前用户 assistant 消息上的 usage 统计。"""
    since = datetime.now() - timedelta(days=days)
    rows = db.scalars(
        select(AIMessage).where(
            AIMessage.role == "assistant",
            AIMessage.usage.is_not(None),
            AIMessage.created_at >= since,
            AIMessage.conversation_id.in_(
                select(AIConversation.id).where(AIConversation.user_id == user_id)
            ),
        )
    ).all()
    by_day: dict[str, int] = {}
    by_model: dict[str, int] = {}
    prompt_total = completion_total = requests = 0
    for m in rows:
        u = m.usage or {}
        tokens = int(u.get("total_tokens") or 0)
        prompt_total += int(u.get("prompt_tokens") or 0)
        completion_total += int(u.get("completion_tokens") or 0)
        requests += 1
        day = m.created_at.strftime("%Y-%m-%d")
        by_day[day] = by_day.get(day, 0) + tokens
        model = str(u.get("model") or "unknown")
        by_model[model] = by_model.get(model, 0) + tokens
    return {
        "requests": requests,
        "prompt_tokens": prompt_total,
        "completion_tokens": completion_total,
        "total_tokens": prompt_total + completion_total,
        "by_day": [{"date": d, "tokens": t} for d, t in sorted(by_day.items())],
        "by_model": [{"model": k, "tokens": v} for k, v in sorted(by_model.items(), key=lambda x: -x[1])],
    }


def delete_conversation(db: Session, conversation_id: int, operator) -> None:
    conv = _get_own_conversation(db, conversation_id, operator.id)
    conv.status = 2  # 软删除
    db.commit()
    write_log(db, user_id=operator.id, username=operator.username, module="AI助手",
              action="删除会话", params={"id": conv.id}, result=1)


def update_conversation(
    db: Session,
    conversation_id: int,
    operator,
    *,
    title: str | None = None,
    pinned: bool | None = None,
) -> dict:
    """会话编辑（M7）：重命名 title / 置顶 pinned，仅本人会话，至少提供一项。"""
    if title is None and pinned is None:
        raise HTTPException(status_code=422, detail="无可更新字段")
    conv = _get_own_conversation(db, conversation_id, operator.id)
    if title is not None:
        new_title = title.strip() or conv.title
        dup = (
            db.query(AIConversation)
            .filter(
                AIConversation.user_id == operator.id,
                AIConversation.status == 1,
                AIConversation.id != conv.id,
                AIConversation.title == new_title,
            )
            .first()
        )
        if dup:
            raise HTTPException(status_code=422, detail="已存在同名会话，请换个名称")
        conv.title = new_title
    if pinned is not None:
        conv.pinned = 1 if pinned else 0
    db.commit()
    params: dict = {"id": conv.id}
    if title is not None:
        params["title"] = conv.title
    if pinned is not None:
        params["pinned"] = conv.pinned
    write_log(db, user_id=operator.id, username=operator.username, module="AI助手",
              action="更新会话", params=params, result=1)
    return serialize_conversation(conv)
