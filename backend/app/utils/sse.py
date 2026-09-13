"""SSE 传输层公共工具：长空闲心跳，防止反向代理空闲超时掐断流式连接。

AI 问答链路在工具执行 / LLM 决策阶段可能数十秒无事件下发，nginx 等代理默认
60s 空闲超时会直接断开连接。心跳采用 SSE 注释行（": ping"），是协议标准中
客户端必须忽略的行，前端 sse.ts 解析无 data 行的块时返回 null 自动跳过，
前后端零协议改动。
"""
import asyncio
from typing import AsyncIterator

HEARTBEAT_INTERVAL_SECONDS = 15.0


async def with_heartbeat(
    source: AsyncIterator[str], interval: float = HEARTBEAT_INTERVAL_SECONDS
) -> AsyncIterator[str]:
    """包裹 SSE 事件异步生成器：相邻事件间隔超过 interval 时插入注释行心跳。

    事件与心跳通过 asyncio.wait 竞争等待：事件到达即转发，超时则发心跳并继续
    等待同一个未完成的事件任务（不会打断正在进行的 LLM 流式请求）。

    消费端（StreamingResponse）因客户端断连退出时，按挂起位置收尾：
    - 等待事件途中：取消挂起的 __anext__ 任务，把取消传播进源生成器——
      其内部 with 块与在途 LLM 调用随之真正中断（与原直连行为一致）；
    - 挂起在 yield 边界：显式 aclose 源生成器，确定性触发其清理逻辑。
    """
    it = source.__aiter__()
    pending: asyncio.Task | None = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(it.__anext__())
            done, _ = await asyncio.wait({pending}, timeout=interval)
            if not done:
                yield ": ping\n\n"
                continue
            pending = None
            try:
                yield done.pop().result()
            except StopAsyncIteration:
                return
    finally:
        if pending is not None:
            pending.cancel()
        else:
            await source.aclose()
