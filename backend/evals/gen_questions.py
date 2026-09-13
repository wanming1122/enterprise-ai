"""LLM 起草候选评测问题：逐切片生成 (question, answer) 草稿，供人工校对后并入金标集。

产出的 question_drafts.jsonl 只是草稿，不直接进入 golden_set.jsonl——
每条必须人工确认问题成立、参考答案正确，并补齐金标切片标注后手工整理。

用法（backend 目录下，需可用的默认 LLM 与语料快照）：
    python -m evals.gen_questions --per-chunk 1 --kb-ids 11,12
"""
import argparse
import json
import re

from app.services import llm_client

from evals.config import DATA_DIR, SNAPSHOT_PATH

DRAFT_PROMPT = (
    "以下是一段企业制度/文档切片。请站在企业员工角度，提出一个该切片内容能够回答的自然问题。\n"
    "要求：用与原文不同的措辞（不要照抄原句），问题应具体、脱离本切片也能独立理解。\n"
    "只输出 JSON 对象：{\"question\": \"...\", \"answer\": \"...\"}，"
    "answer 为依据本切片的简短参考答案（不超过60字）。\n\n切片内容：\n{content}"
)


def _parse_json_obj(raw: str) -> dict:
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("回复中未找到 JSON 对象")
    return json.loads(text[start:end + 1])


def gen(per_chunk: int, kb_ids: list[int] | None) -> None:
    snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    chunks = [c for c in snapshot["chunks"] if not kb_ids or c["kb_id"] in kb_ids]
    out_path = DATA_DIR / "question_drafts.jsonl"
    drafts, failed = [], 0
    for c in chunks:
        for _ in range(per_chunk):
            try:
                raw = llm_client.chat_once(
                    [{"role": "user", "content": DRAFT_PROMPT.format(content=c["content"])}],
                    max_tokens=300, temperature=0.7,
                )
                obj = _parse_json_obj(raw)
            except Exception as exc:  # 单条失败不阻断整批
                failed += 1
                print(f"[跳过] kb{c['kb_id']}/f{c['file_id']}c{c['chunk_index']}：{exc}")
                continue
            drafts.append({
                "kb_id": c["kb_id"], "file_id": c["file_id"], "chunk_index": c["chunk_index"],
                "file_name": c["file_name"],
                "question": str(obj.get("question", "")).strip(),
                "reference_answer": str(obj.get("answer", "")).strip(),
                "status": "待人工校对",
            })
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        for d in drafts:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    print(f"草稿 {len(drafts)} 条（失败 {failed} 条）已写入 {out_path}")
    print("注意：草稿必须逐条人工校对、补齐金标切片标注后，才可整理进 golden_set.jsonl")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LLM 起草候选评测问题")
    parser.add_argument("--per-chunk", type=int, default=1, help="每个切片生成几条候选")
    parser.add_argument("--kb-ids", type=str, default="", help="限定知识库 id，逗号分隔；留空为全部")
    args = parser.parse_args()
    kb_ids = [int(x) for x in re.split(r"[,\s]+", args.kb_ids.strip()) if x] or None
    gen(args.per_chunk, kb_ids)
