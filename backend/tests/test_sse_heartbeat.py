"""SSE 心跳包裹器（app/utils/sse.py）单测：事件透传、静默期心跳、断连取消传播。

纯逻辑测试（不发起真实 LLM / 数据库请求），时间参数留有充足裕量避免抖动。
"""
import asyncio
from contextlib import suppress as _suppress

from app.utils.sse import with_heartbeat


async def _collect(agen, limit=100):
    out = []
    async for chunk in agen:
        out.append(chunk)
        if len(out) >= limit:
            break
    return out


def test_with_heartbeat_forwards_events_without_ping():
    """事件间隔小于心跳周期：全部事件按序透传，不插入心跳。"""

    async def source():
        for i in range(3):
            yield f"event: e{i}\n\n"

    async def run():
        return await _collect(with_heartbeat(source(), interval=0.05))

    out = asyncio.run(run())
    assert out == [f"event: e{i}\n\n" for i in range(3)]


def test_with_heartbeat_emits_ping_during_silence():
    """静默期超过心跳周期：插入 ': ping' 注释行，事件顺序保持不变。"""

    async def source():
        yield "event: start\n\n"
        await asyncio.sleep(0.08)  # 静默期（模拟工具执行/LLM 决策）
        yield "event: end\n\n"

    async def run():
        return await _collect(with_heartbeat(source(), interval=0.02))

    out = asyncio.run(run())
    assert any(c.startswith(": ping") for c in out), "静默期应插入心跳注释行"
    assert out.index("event: start\n\n") < out.index("event: end\n\n")
    assert out[-1] == "event: end\n\n"


def test_with_heartbeat_close_propagates_cancellation_to_source():
    """消费端在等待事件途中退出（真实断连时机）：取消传播进源生成器挂起的 await。"""
    released = asyncio.Event()

    async def source():
        try:
            yield "event: first\n\n"
            await asyncio.Event().wait()  # 模拟长 LLM 调用，永不完成
        except asyncio.CancelledError:
            released.set()
            raise

    async def run():
        # interval 取 1s：测试窗口内不产生心跳，拉取任务确定性地阻塞在事件等待期
        agen = with_heartbeat(source(), interval=1.0)
        first = await agen.__anext__()
        assert first == "event: first\n\n"
        # 消费端发起下一次拉取（包裹器进入 wait，源进入长 await），随后被取消——
        # 等价于 StreamingResponse 的响应任务在等待下一块时因客户端断连被取消
        nxt = asyncio.ensure_future(agen.__anext__())
        await asyncio.sleep(0.05)
        nxt.cancel()
        with _suppress(asyncio.CancelledError):
            await nxt
        for _ in range(20):  # 给被取消的源任务事件循环周期完成清理
            if released.is_set():
                break
            await asyncio.sleep(0.01)
        return released.is_set()

    assert asyncio.run(run())


def test_with_heartbeat_close_at_yield_closes_source():
    """消费端在 yield 边界关闭生成器：源生成器被显式 aclose（清理逻辑确定性执行）。"""
    closed = asyncio.Event()

    async def source():
        try:
            yield "event: first\n\n"
            yield "event: second\n\n"
        finally:
            closed.set()

    async def run():
        agen = with_heartbeat(source(), interval=0.02)
        first = await agen.__anext__()
        assert first == "event: first\n\n"
        await agen.aclose()
        return closed.is_set()

    assert asyncio.run(run())
