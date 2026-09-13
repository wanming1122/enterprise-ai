"""模型配置初始化脚本：智谱 rerank（b3 评测用，设为默认）+ DeepSeek 对话模型（备用，不设默认）。

幂等：同类型同模型名的启用配置已存在则跳过。前置条件（backend/.env）：
- ZHIPU_API_KEY：智谱 rerank 探针与配置必需，探针失败则中止（不写无效配置）；
- DEEPSEEK_API_KEY：缺失时跳过 DeepSeek 配置。

rerank 端点契约（Cohere/Jina 风格，与 app/services/kb_rag_service._rerank 一致）：
POST {base}/rerank  {model, query, documents, top_n} → results[{index, relevance_score}]。
探针使用金标集真实问题与真实切片，先 rerank-2 后 rerank，取首个通过契约校验的模型名。

用法（backend 目录下）：python -m evals.setup_models
"""
import json
import time

import httpx
from dotenv import dotenv_values
from sqlalchemy import select

from app.models.ai import AIModel
from app.models.user import SysUser
from app.schemas.ai_model import AIModelCreate
from app.services import ai_model_service

from evals.config import GOLDEN_SET_PATH, SNAPSHOT_PATH

ZHIPU_BASE = "https://open.bigmodel.cn/api/paas/v4"
DEEPSEEK_BASE = "https://api.deepseek.com"


def _probe_rerank(model_name: str, api_key: str, query: str, documents: list[str]) -> dict | None:
    """返回 {"model_name", "latency_ms"} 表示探针通过；契约不符或失败返回 None。"""
    started = time.perf_counter()
    try:
        with httpx.Client(timeout=30) as client:
            r = client.post(
                f"{ZHIPU_BASE}/rerank",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"model": model_name, "query": query,
                      "documents": [d[:2000] for d in documents], "top_n": len(documents)},
            )
    except Exception as exc:  # noqa: BLE001 探针把网络异常也视为未通过
        print(f"  [{model_name}] 请求异常：{exc}")
        return None
    latency_ms = int((time.perf_counter() - started) * 1000)
    if r.status_code != 200:
        print(f"  [{model_name}] HTTP {r.status_code}：{r.text[:200]}")
        return None
    results = r.json().get("results")
    if not isinstance(results, list) or not results:
        print(f"  [{model_name}] 响应缺少 results：{str(r.json())[:200]}")
        return None
    first = results[0]
    if "index" not in first or "relevance_score" not in first:
        print(f"  [{model_name}] results 项缺少 index/relevance_score：{first}")
        return None
    return {"model_name": model_name, "latency_ms": latency_ms}


def _probe_deepseek(api_key: str) -> bool:
    try:
        with httpx.Client(timeout=30) as client:
            r = client.post(
                f"{DEEPSEEK_BASE}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"model": "deepseek-chat", "messages": [{"role": "user", "content": "回复ok"}],
                      "max_tokens": 8, "temperature": 0},
            )
    except Exception as exc:  # noqa: BLE001
        print(f"  [deepseek-chat] 请求异常：{exc}")
        return False
    if r.status_code != 200:
        print(f"  [deepseek-chat] HTTP {r.status_code}：{r.text[:200]}")
        return False
    print(f"  [deepseek-chat] 连通正常：{(r.json()['choices'][0]['message'].get('content') or '')[:20]}")
    return True


def _ensure_model(db, *, name: str, model_type: str, provider: str, base_url: str | None,
                  model_name: str, api_key: str, is_default: bool, remark: str, operator) -> None:
    exists = db.scalar(
        select(AIModel).where(AIModel.model_type == model_type,
                              AIModel.model_name == model_name, AIModel.status != 2)
    )
    if exists:
        print(f"  [跳过] {model_type}/{model_name} 已存在（id={exists.id}, is_default={exists.is_default}）")
        return
    data = AIModelCreate(name=name, model_type=model_type, provider=provider, base_url=base_url,
                         api_key=api_key, model_name=model_name, temperature=None,
                         context_window=None, remark=remark, is_default=is_default, status=1)
    created = ai_model_service.create_model(db, data, operator)
    print(f"  [新增] id={created['id']} {name}（{model_type}/{model_name}，默认={is_default}）")


def _probe_inputs() -> tuple[str, list[str]]:
    """探针素材：金标集第一道事实型问题 + 快照中该题 gold 库的 3 条真实切片。"""
    question = "出差住宿的报销标准是多少？"
    documents = ["报销标准示例一", "报销标准示例二", "报销标准示例三"]
    try:
        questions = [json.loads(l) for l in GOLDEN_SET_PATH.open(encoding="utf-8") if l.strip()]
        first_ir = next((q for q in questions if q.get("question_type") != "negative"), None)
        if first_ir:
            question = first_ir["question"]
        snap = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
        gold_kb = first_ir["kb_ids"][0] if first_ir and first_ir.get("kb_ids") else None
        chunks = [c["content"] for c in snap["chunks"]
                  if gold_kb is not None and c["kb_id"] == gold_kb][:3]
        if len(chunks) == 3:
            documents = chunks
    except Exception as exc:  # noqa: BLE001 素材缺失不阻塞探针，退回静态样例
        print(f"  [探针素材] 读取金标集/快照失败，退回静态样例：{exc}")
    return question, documents


def main() -> None:
    cfg = dotenv_values(".env")
    zhipu_key = cfg.get("ZHIPU_API_KEY", "")
    deepseek_key = cfg.get("DEEPSEEK_API_KEY", "")

    if zhipu_key:
        question, documents = _probe_inputs()
        print(f"[探针] 智谱 /rerank（问题：{question[:24]}…，文档 {len(documents)} 条）")
        probe = None
        for model_name in ("rerank-2", "rerank"):
            probe = _probe_rerank(model_name, zhipu_key, question, documents)
            if probe:
                break
        if not probe:
            raise SystemExit("智谱 rerank 探针未通过（rerank-2 / rerank 均失败），未写入任何配置")
        print(f"  [通过] {probe['model_name']}（{probe['latency_ms']}ms）")
    else:
        probe = None
        print("[探针] .env 无 ZHIPU_API_KEY，跳过 rerank")

    from app.db.session import SessionLocal
    db = SessionLocal()
    try:
        admin = db.scalar(select(SysUser).where(SysUser.username == "admin"))
        if admin is None:
            raise SystemExit("未找到 admin 账号，无法写操作日志")
        if probe:
            _ensure_model(db, name="智谱重排", model_type="rerank", provider="zhipu",
                          base_url=None, model_name=probe["model_name"], api_key=zhipu_key,
                          is_default=True, remark="检索评测 b3 配置（evals/setup_models.py 写入）",
                          operator=admin)
        else:
            print("[跳过] rerank 配置未写入（探针未通过或无 key）")
        if deepseek_key:
            print("[探针] DeepSeek /chat/completions")
            if _probe_deepseek(deepseek_key):
                _ensure_model(db, name="DeepSeek Chat", model_type="llm", provider="openai_compatible",
                              base_url=DEEPSEEK_BASE, model_name="deepseek-chat", api_key=deepseek_key,
                              is_default=False, remark="备用生成模型（evals/setup_models.py 写入，不设默认）",
                              operator=admin)
            else:
                print("[跳过] DeepSeek 探针未通过，未写入配置")
        else:
            print("[跳过] .env 无 DEEPSEEK_API_KEY，未建 DeepSeek 配置")
    finally:
        db.close()
    print("完成。")


if __name__ == "__main__":
    main()
