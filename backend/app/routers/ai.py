"""AI助手路由（M4-T3）：SSE 流式问答、会话列表/详情/软删、记忆管理、用量汇总。"""
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask
from sqlalchemy.orm import Session

from app.core.deps import require_permissions
from app.db.session import get_db
from app.models.user import SysUser
from app.schemas.ai import AIChatIn, AIConversationPatchIn
from app.schemas.ai_memory import AIMemoryUpdate
from app.services import ai_chat_service, ai_memory_service, ai_model_service
from app.services.menu_service import collect_permissions
from app.utils.page import page_result
from app.utils.response import ok
from app.utils.sse import with_heartbeat

router = APIRouter(prefix="/api/v1/ai", tags=["AI助手"])


@router.post("/chat")
async def chat(
    data: AIChatIn,
    request: Request,
    operator: SysUser = Depends(require_permissions("ai:chat")),
    db: Session = Depends(get_db),
):
    """AI助手流式问答（LangGraph Agent：agent⇄tools[retrieve/nl2sql/server_admin]→generate）。

    支持图片多模态；仅持 ai:server_admin 权限的账号注入服务器管理工具；
    kb_ids/source 供知识库问答调试页复用本链路（检索范围限定 + 会话来源标记）。
    """
    enable_server_admin = "ai:server_admin" in set(collect_permissions(db, operator.id))
    # 异步记忆提取：流结束后由 BackgroundTask 执行（客户端 done 后断开也不影响）
    holder: dict = {}
    # with_heartbeat：工具执行/LLM 决策的长静默期插入注释行心跳，防反代空闲超时掐断
    return StreamingResponse(
        with_heartbeat(
            ai_chat_service.chat_sse(
                operator.id, operator.username, question=data.question.strip(),
                conversation_id=data.conversation_id, deep_thinking=data.deep_thinking,
                images=data.images, enable_server_admin=enable_server_admin, result_holder=holder,
                model_id=data.model_id, kb_ids=data.kb_ids, source=data.source, request=request,
            )
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        background=BackgroundTask(ai_memory_service.finalize_round, holder, operator.id, operator.username),
    )


@router.get("/enabled-models")
def list_enabled_models(
    operator: SysUser = Depends(require_permissions("ai:chat")),
    db: Session = Depends(get_db),
):
    """启用的生成模型列表（AI助手模型切换下拉）：不含 api_key 等敏感字段。

    注意：/ai/models 已被模型配置管理页（prefix=/api/v1/ai/models）占用，故用 enabled-models。
    """
    return ok(ai_model_service.list_enabled_llm_models(db))


@router.get("/conversations")
def list_conversations(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    source: str = Query(default="ai"),
    operator: SysUser = Depends(require_permissions("ai:chat")),
    db: Session = Depends(get_db),
):
    """我的会话分页列表（排除软删；source=ai/kb 区分 AI 助手与问答调试）。"""
    items, total = ai_chat_service.list_conversations(
        db, user_id=operator.id, page=page, page_size=page_size, source=source
    )
    return ok(page_result(items, total, page, page_size))


@router.get("/conversations/{conversation_id}")
def get_conversation(
    conversation_id: int,
    operator: SysUser = Depends(require_permissions("ai:chat")),
    db: Session = Depends(get_db),
):
    """会话详情与全部消息（仅本人会话）。"""
    return ok(ai_chat_service.get_conversation(db, conversation_id, operator))


@router.patch("/conversations/{conversation_id}")
def update_conversation(
    conversation_id: int,
    data: AIConversationPatchIn,
    operator: SysUser = Depends(require_permissions("ai:chat")),
    db: Session = Depends(get_db),
):
    """会话编辑：重命名（title）与置顶（pinned）切换，仅本人会话。"""
    return ok(
        ai_chat_service.update_conversation(
            db, conversation_id, operator, title=data.title, pinned=data.pinned
        )
    )


@router.delete("/conversations/{conversation_id}")
def delete_conversation(
    conversation_id: int,
    operator: SysUser = Depends(require_permissions("ai:chat")),
    db: Session = Depends(get_db),
):
    """删除会话（软删除）。"""
    ai_chat_service.delete_conversation(db, conversation_id, operator)
    return ok(message="删除成功")


@router.get("/conversations/{conversation_id}/messages")
def list_messages_before(
    conversation_id: int,
    before_id: int = Query(..., description="返回该 ID 之前的更早消息"),
    limit: int = Query(default=100, ge=1, le=200),
    operator: SysUser = Depends(require_permissions("ai:chat")),
    db: Session = Depends(get_db),
):
    """会话更早消息分页（长会话不一次性全量拉取）。"""
    return ok(ai_chat_service.list_messages_before(
        db, conversation_id, operator, before_id=before_id, limit=limit
    ))


@router.get("/usage/summary")
def usage_summary(
    days: int = Query(default=30, ge=1, le=365),
    operator: SysUser = Depends(require_permissions("ai:chat")),
    db: Session = Depends(get_db),
):
    """本人用量汇总（成本监控）：最近 N 天 tokens 按天/按模型聚合。"""
    return ok(ai_chat_service.usage_summary(db, operator.id, days=days))


# ---------- 长期记忆管理 ----------


@router.get("/memories")
def list_memories(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    operator: SysUser = Depends(require_permissions("ai:chat")),
    db: Session = Depends(get_db),
):
    """我的长期记忆列表（仅本人，按更新时间倒序）。"""
    items, total = ai_memory_service.list_memories(db, operator.id, page=page, page_size=page_size)
    return ok(page_result(items, total, page, page_size))


@router.patch("/memories/{memory_id}")
def update_memory(
    memory_id: int,
    data: AIMemoryUpdate,
    operator: SysUser = Depends(require_permissions("ai:chat")),
    db: Session = Depends(get_db),
):
    """编辑本人记忆内容（重新向量化）。"""
    return ok(ai_memory_service.edit_memory(db, memory_id, operator, data.content))


@router.delete("/memories/{memory_id}")
def delete_memory(
    memory_id: int,
    operator: SysUser = Depends(require_permissions("ai:chat")),
    db: Session = Depends(get_db),
):
    """删除本人单条记忆（软删除并移除向量）。"""
    ai_memory_service.delete_memory(db, memory_id, operator)
    return ok(message="已删除")


@router.delete("/memories")
def clear_memories(
    operator: SysUser = Depends(require_permissions("ai:chat")),
    db: Session = Depends(get_db),
):
    """清空本人全部长期记忆。"""
    count = ai_memory_service.clear_memories(db, operator)
    return ok(message=f"已清空 {count} 条记忆", data={"count": count})
