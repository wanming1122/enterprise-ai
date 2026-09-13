"""检索评测主入口：金标集 → 消融配置跑检索 → 计算 IR 指标 → 输出报告与逐题明细。

用法（backend 目录下）：
    python -m evals.dump_snapshot                     # 先导语料快照
    python -m evals.run_eval                          # 默认跑 b0,b1,b2
    python -m evals.run_eval --configs b0,b1,b2,b4    # 含 CRAG（需默认 LLM）
    python -m evals.run_eval --limit 3                # 只跑前 3 题（试跑）
    python -m evals.run_eval --tag baseline           # 报告文件名标签

产物：evals/reports/report_<tag>_<时间戳>.md 与 per_question_<tag>_<时间戳>.jsonl
负例（question_type=negative）不参与 IR 指标，仅随集保留供二期 QA/拒答评测。
"""
import argparse
import json
import time
from datetime import datetime

from sqlalchemy import func, select

from app.db.session import SessionLocal
from app.models.kb import KBChunk, KBKnowledgeBase
from app.services import ai_model_service, kb_rag_service, llm_client

from evals.config import CONFIG_DESC, GOLDEN_SET_PATH, K_VALUES, REPORTS_DIR, SNAPSHOT_PATH
from evals.metrics import (
    hit_rate_at_k,
    mean,
    mrr_at_k,
    ndcg_at_k,
    percentile,
    precision_at_k,
    recall_at_k,
)

METRIC_FNS = {"Recall": recall_at_k, "Precision": precision_at_k,
              "HitRate": hit_rate_at_k, "MRR": mrr_at_k, "NDCG": ndcg_at_k}
STAGE_CONFIGS = ("b0", "b1", "b2", "b3")  # 共享一次 retrieve_with_stages 调用


def chunk_key(r: dict) -> tuple:
    return (r["kb_id"], r["file_id"], r["chunk_index"])


class CallCounter:
    """给 kb_rag_service.embed_texts 与 llm_client.chat_once 挂计数器，统计 API 调用量。"""

    def __init__(self):
        self.embed = 0
        self.llm = 0
        self._orig_embed = kb_rag_service.embed_texts
        self._orig_chat = llm_client.chat_once
        kb_rag_service.embed_texts = self._embed
        llm_client.chat_once = self._chat

    def _embed(self, *args, **kwargs):
        self.embed += 1
        return self._orig_embed(*args, **kwargs)

    def _chat(self, *args, **kwargs):
        self.llm += 1
        return self._orig_chat(*args, **kwargs)

    def restore(self):
        kb_rag_service.embed_texts = self._orig_embed
        llm_client.chat_once = self._orig_chat


def load_questions() -> list[dict]:
    questions = []
    with GOLDEN_SET_PATH.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                questions.append(json.loads(line))
    questions.sort(key=lambda q: q["id"])
    return questions


def precheck(questions: list[dict]) -> None:
    """校验金标引用的切片都存在于快照；Chroma 与 MySQL 切片数不一致时告警。"""
    snap = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    snap_keys = {(c["kb_id"], c["file_id"], c["chunk_index"]) for c in snap["chunks"]}
    missing = []
    for q in questions:
        for rel in q.get("relevant", []):
            key = (rel["kb_id"], rel["file_id"], rel["chunk_index"])
            if key not in snap_keys:
                missing.append(f"{q['id']}:{key}")
    if missing:
        raise SystemExit(f"金标集引用了快照中不存在的切片（请重跑 dump_snapshot 并复核标注）：{missing[:5]}")

    db = SessionLocal()
    try:
        kbs = db.scalars(
            select(KBKnowledgeBase).where(KBKnowledgeBase.status != 2)
        ).all()
        warnings = []
        for kb in kbs:
            col = kb_rag_service._get_collection(kb, create=False)
            db_count = db.scalar(
                select(func.count()).select_from(KBChunk)
                .where(KBChunk.kb_id == kb.id, KBChunk.status != 2)
            ) or 0
            vec_count = col.count() if col is not None else 0
            if vec_count != db_count:
                warnings.append(f"kb{kb.id}({kb.name}): Chroma {vec_count} != MySQL {db_count}")
        if warnings:
            print("[警告] 向量库与数据库切片数不一致，请先在页面执行「重建索引」：")
            for w in warnings:
                print(f"  - {w}")
    finally:
        db.close()


def _score(candidates: list[dict], relevant: dict, elapsed_ms: float,
           embed_calls: int, llm_calls: int, ks: tuple) -> dict:
    keys = [chunk_key(r) for r in candidates]
    metrics = {}
    for k in ks:
        for name, fn in METRIC_FNS.items():
            metrics[f"{name}@{k}"] = fn(keys, relevant, k)
    return {
        "elapsed_ms": round(elapsed_ms, 1),
        "embed_calls": embed_calls,
        "llm_calls": llm_calls,
        "n_candidates": len(keys),
        "metrics": metrics,
        "retrieved": [list(key) for key in keys[: max(ks)]],
    }


def evaluate_question(db, q: dict, active_kb_ids: list[int], configs: list[str],
                      counter: CallCounter, ks: tuple) -> dict:
    """单题评测：b0~b3 共享一次阶段化检索，b4 单独走 CRAG；单题失败不阻断整批。"""
    relevant = {(r["kb_id"], r["file_id"], r["chunk_index"]): r.get("grade", 2)
                for r in q.get("relevant", [])}
    record = {
        "id": q["id"], "question": q["question"], "type": q.get("question_type", "factoid"),
        "relevant": [[k[0], k[1], k[2], g] for k, g in relevant.items()],
        "configs": {},
    }
    if record["type"] == "negative":
        record["note"] = "负例：不参与 IR 指标"
        return record

    try:
        if any(c in STAGE_CONFIGS for c in configs):
            e0, l0 = counter.embed, counter.llm
            t0 = time.perf_counter()
            stages = kb_rag_service.retrieve_with_stages(
                db, query=q["question"], kb_ids=active_kb_ids, top_k=max(ks))
            hybrid_ms = (time.perf_counter() - t0) * 1000
            hybrid_embed, hybrid_llm = counter.embed - e0, counter.llm - l0

            ranked = stages["reranked"]
            cands = {
                "b0": sorted(stages["vector"], key=lambda r: -r["similarity"]),
                "b1": stages["bm25"],
                "b2": stages["fused"],
                "b3": ([r for r in ranked if r["similarity"] >= kb_rag_service.RERANK_MIN_SCORE]
                       if ranked is not None else None),
            }
            for cfg in STAGE_CONFIGS:
                if cfg not in configs:
                    continue
                if cands[cfg] is None:
                    record["configs"][cfg] = {"skipped": "rerank 模型未配置"}
                    continue
                record["configs"][cfg] = _score(cands[cfg], relevant, hybrid_ms,
                                                hybrid_embed, hybrid_llm, ks)

        if "b4" in configs:
            e0, l0 = counter.embed, counter.llm
            t0 = time.perf_counter()
            results, used_query = kb_rag_service.retrieve_with_crag(
                db, query=q["question"], kb_ids=active_kb_ids, top_k=max(ks))
            crag_ms = (time.perf_counter() - t0) * 1000
            record["configs"]["b4"] = _score(results, relevant, crag_ms,
                                             counter.embed - e0, counter.llm - l0, ks)
            record["crag_used_query"] = used_query
    except Exception as exc:  # noqa: BLE001 单题失败落盘后继续
        record["error"] = str(exc)[:200]
    return record


def aggregate(records: list[dict], configs: list[str], ks: tuple) -> dict:
    """按配置聚合：指标均值（剔除负例与失败题）、时延分位、平均调用量。"""
    ir = [r for r in records if not r.get("error") and r["type"] != "negative"]
    agg = {}
    for cfg in configs:
        rows = [r["configs"][cfg] for r in ir
                if cfg in r.get("configs", {}) and "metrics" in r["configs"][cfg]]
        if not rows:
            agg[cfg] = None
            continue
        lat = [row["elapsed_ms"] for row in rows]
        agg[cfg] = {
            "n": len(rows),
            "metrics": {f"{name}@{k}": mean([row["metrics"].get(f"{name}@{k}") for row in rows])
                        for name in METRIC_FNS for k in ks},
            "p50_ms": percentile(lat, 50),
            "p95_ms": percentile(lat, 95),
            "embed_calls_avg": mean([row["embed_calls"] for row in rows]),
            "llm_calls_avg": mean([row["llm_calls"] for row in rows]),
        }
    return agg


def type_breakdown(records: list[dict], cfg: str, k: int) -> dict:
    """指定配置按题型的指标分解。"""
    by_type: dict[str, list[dict]] = {}
    for r in records:
        if r.get("error") or r["type"] == "negative":
            continue
        row = r.get("configs", {}).get(cfg)
        if row and "metrics" in row:
            by_type.setdefault(r["type"], []).append(row)
    return {
        t: {"n": len(rows),
            "recall": mean([x["metrics"].get(f"Recall@{k}") for x in rows]),
            "ndcg": mean([x["metrics"].get(f"NDCG@{k}") for x in rows]),
            "mrr": mean([x["metrics"].get(f"MRR@{k}") for x in rows])}
        for t, rows in by_type.items()
    }


def _fmt(v) -> str:
    return f"{v:.4f}" if isinstance(v, float) else ("-" if v is None else str(v))


def write_report(path, tag, snapshot, questions, used_configs, skipped, agg,
                 breakdown_cfg, breakdown, ks) -> None:
    n_ir = sum(1 for q in questions if q.get("question_type") != "negative")
    n_neg = len(questions) - n_ir
    lines = [
        f"# 检索评测报告（{tag}）", "",
        f"- 评测时间：{datetime.now().isoformat(timespec='seconds')}",
        f"- 语料快照：{snapshot['generated_at']}，有效库 {len(snapshot['kbs'])} / "
        f"文件 {len(snapshot['files'])} / 切片 {len(snapshot['chunks'])}",
        f"- 题目：IR 评测 {n_ir} 题（负例 {n_neg} 题不参与指标）；检索范围：全部有效库联合检索",
        f"- K 档位：{list(ks)}（6 为生产默认 top_k）",
        f"- 检索配置：{'; '.join(f'{c}={CONFIG_DESC[c]}' for c in used_configs)}",
        f"- 模型：embedding={snapshot['env']['embedding']}，rerank={snapshot['env']['rerank']}，"
        f"llm={snapshot['env']['llm']}",
    ]
    if skipped:
        lines.append(f"- 跳过配置：{'; '.join(skipped)}")
    lines += ["", "## 主表（K=6）", "",
              "| 配置 | 题数 | Recall | NDCG | MRR | HitRate | Precision | p50(ms) | p95(ms) | embed/题 | llm/题 |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for cfg in used_configs:
        a = agg.get(cfg)
        if not a:
            lines.append(f"| {cfg} | - | - | - | - | - | - | - | - | - | - |")
            continue
        m = a["metrics"]
        lines.append(
            f"| {cfg} | {a['n']} | {_fmt(m.get('Recall@6'))} | {_fmt(m.get('NDCG@6'))} | "
            f"{_fmt(m.get('MRR@6'))} | {_fmt(m.get('HitRate@6'))} | {_fmt(m.get('Precision@6'))} | "
            f"{_fmt(a['p50_ms'])} | {_fmt(a['p95_ms'])} | "
            f"{_fmt(a['embed_calls_avg'])} | {_fmt(a['llm_calls_avg'])} |")

    lines += ["", "## Recall / NDCG 随 K 变化", "",
              "| 配置 | " + " | ".join(f"{n}@{k}" for k in ks for n in ("Recall", "NDCG")) + " |",
              "| --- | " + " | ".join("---" for _ in ks for _ in range(2)) + " |"]
    for cfg in used_configs:
        a = agg.get(cfg)
        if not a:
            continue
        cells = " | ".join(_fmt(a["metrics"].get(f"{n}@{k}")) for k in ks for n in ("Recall", "NDCG"))
        lines.append(f"| {cfg} | {cells} |")

    lines += ["", f"## 按题型分解（{breakdown_cfg}，K=6）", "",
              "| 题型 | 题数 | Recall@6 | NDCG@6 | MRR@6 |", "| --- | --- | --- | --- | --- |"]
    for t, row in sorted(breakdown.items()):
        lines.append(f"| {t} | {row['n']} | {_fmt(row['recall'])} | {_fmt(row['ndcg'])} | {_fmt(row['mrr'])} |")

    lines += [
        "", "## 说明与已知口径", "",
        "- b0 跨库候选按余弦相似度直接排序（同一 embedding 模型同一向量空间）；"
        "b0/b1/b2 共享同一次混合检索调用的时延，代表整管线开销，单独部署某一路时延更低。",
        "- b4（CRAG）以 top_k=max(K) 调用以支持 K 档评测；分级 prompt 会看到全部候选，"
        "与生产默认 top_k=6 存在轻微口径差异。",
        "- b3 未配置 rerank 模型时自动跳过；b4 未配置默认 LLM 时自动跳过。",
        "- 性能口径：查询向量按（embedding 模型, 维度）分组嵌入，同组一次调用，"
        "调用量见主表 embed/题（分组数取决于语料中记录的模型名是否一致）。",
        "- 逐题命中明细与 badcase 见同目录 per_question_*.jsonl（retrieved 字段为各配置 top10 键序）。",
        "- 负例仅用于二期 QA/拒答评测，不参与本报告指标。",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="RAG 检索评测")
    parser.add_argument("--configs", type=str, default="b0,b1,b2",
                        help="逗号分隔：b0,b1,b2,b3,b4")
    parser.add_argument("--k", type=str, default=",".join(str(x) for x in K_VALUES))
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 题（0=全部）")
    parser.add_argument("--tag", type=str, default="run", help="报告文件名标签")
    args = parser.parse_args()
    ks = tuple(sorted(int(x) for x in args.k.split(",") if x.strip()))
    requested = [c.strip() for c in args.configs.split(",") if c.strip()]
    unknown = [c for c in requested if c not in CONFIG_DESC]
    if unknown:
        raise SystemExit(f"未知配置：{unknown}，可选：{list(CONFIG_DESC)}")

    questions = load_questions()
    if args.limit > 0:
        questions = questions[:args.limit]
    precheck(questions)

    db = SessionLocal()
    counter = CallCounter()
    skipped: list[str] = []
    try:
        embed_cfg = ai_model_service.resolve_embedding_config(db=db)
        try:
            llm_cfg = ai_model_service.resolve_llm_config()
        except Exception:
            llm_cfg = {}
        rerank_cfg = ai_model_service.resolve_rerank_config(db=db)

        configs = list(requested)
        if "b3" in configs and not (rerank_cfg and rerank_cfg.get("api_key")):
            configs.remove("b3")
            skipped.append("b3（rerank 未配置）")
        if "b4" in configs and not (llm_cfg and llm_cfg.get("api_key")):
            configs.remove("b4")
            skipped.append("b4（默认 LLM 未配置）")
        if not configs:
            raise SystemExit("没有可运行的配置")

        active_kb_ids = sorted({c["kb_id"] for c in
                                json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))["chunks"]})
        records = []
        t_start = time.perf_counter()
        for i, q in enumerate(questions, start=1):
            record = evaluate_question(db, q, active_kb_ids, configs, counter, ks)
            records.append(record)
            flag = "ERR" if record.get("error") else "ok"
            print(f"[{i}/{len(questions)}] {flag} {q['id']} {q['question'][:24]}")
        wall = time.perf_counter() - t_start

        snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
        snapshot["env"] = {
            "embedding": embed_cfg.get("model_name", "?"),
            "rerank": rerank_cfg.get("model_name") if rerank_cfg else "未配置",
            "llm": llm_cfg.get("model_name", "?") if llm_cfg else "未配置",
        }
        agg = aggregate(records, configs, ks)
        breakdown_cfg = "b2" if "b2" in configs else configs[0]
        breakdown = type_breakdown(records, breakdown_cfg, 6)

        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_path = REPORTS_DIR / f"report_{args.tag}_{ts}.md"
        detail_path = REPORTS_DIR / f"per_question_{args.tag}_{ts}.jsonl"
        write_report(report_path, args.tag, snapshot, questions, configs, skipped,
                     agg, breakdown_cfg, breakdown, ks)
        with detail_path.open("w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

        print(f"\n完成：{len(records)} 题，总耗时 {wall:.1f}s，"
              f"embedding 调用 {counter.embed} 次，LLM 调用 {counter.llm} 次")
        print(f"报告：{report_path}")
        print(f"逐题明细：{detail_path}")
        for cfg in configs:
            a = agg.get(cfg)
            if a:
                m = a["metrics"]
                print(f"  {cfg}: Recall@6={_fmt(m.get('Recall@6'))} NDCG@6={_fmt(m.get('NDCG@6'))} "
                      f"MRR@6={_fmt(m.get('MRR@6'))} p50={_fmt(a['p50_ms'])}ms")
    finally:
        counter.restore()
        db.close()


if __name__ == "__main__":
    main()
