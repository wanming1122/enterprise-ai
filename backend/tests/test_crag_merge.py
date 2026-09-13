"""CRAG 改写轮加权并集合并纯函数测试：不触库、不调 LLM（去重 / 加权名次融合 / 防挤占 / 截断）。"""
from app.services.kb_rag_service import merge_round_results


def _chunk(kb_id: int, file_id: int, chunk_index: int, tag: str) -> dict:
    return {"kb_id": kb_id, "file_id": file_id, "chunk_index": chunk_index, "content": tag}


class TestMergeRoundResults:
    def test_dedup_and_fuse(self):
        # 两轮都命中的切片获得双份加权 RRF 分，排在单轮独有切片之前
        a = [_chunk(1, 1, 0, "a0"), _chunk(1, 1, 1, "a1")]
        b = [_chunk(1, 1, 1, "a1"), _chunk(1, 1, 2, "a2")]
        merged = merge_round_results(a, b, top_n=10)
        keys = [(r["kb_id"], r["file_id"], r["chunk_index"]) for r in merged]
        assert keys == [(1, 1, 1), (1, 1, 0), (1, 1, 2)]

    def test_round1_head_not_displaced(self):
        # q017/v3 场景：改写轮（b）整体换血时，原查询头部切片不得被挤出 top_n
        a = [_chunk(1, 1, i, f"a{i}") for i in range(6)]
        b = [_chunk(2, 2, i, f"b{i}") for i in range(6)]
        merged6 = merge_round_results(a, b, top_n=6)
        keys6 = [(r["kb_id"], r["file_id"], r["chunk_index"]) for r in merged6]
        assert keys6 == [(1, 1, i) for i in range(6)]  # 原查询轮加权后头部完整保留
        # 截断放宽时，改写轮切片在余量内补充
        keys12 = {(r["kb_id"], r["file_id"], r["chunk_index"])
                  for r in merge_round_results(a, b, top_n=12)}
        assert {(2, 2, 0), (2, 2, 1)} <= keys12

    def test_round2_fills_when_round1_short(self):
        # 原查询轮结果不足 top_n 时，改写轮命中按名次补入
        a = [_chunk(1, 1, 0, "a0"), _chunk(1, 1, 1, "a1")]
        b = [_chunk(2, 2, i, f"b{i}") for i in range(6)]
        merged = merge_round_results(a, b, top_n=6)
        keys = [(r["kb_id"], r["file_id"], r["chunk_index"]) for r in merged]
        assert keys == [(1, 1, 0), (1, 1, 1), (2, 2, 0), (2, 2, 1), (2, 2, 2), (2, 2, 3)]

    def test_top_n_truncation(self):
        a = [_chunk(1, 1, i, "a") for i in range(10)]
        b = [_chunk(2, 2, i, "b") for i in range(10)]
        assert len(merge_round_results(a, b, top_n=4)) == 4

    def test_empty_round(self):
        a = [_chunk(1, 1, 0, "a0")]
        assert merge_round_results(a, [], 5)[0]["content"] == "a0"
        assert merge_round_results([], a, 5)[0]["content"] == "a0"
        assert merge_round_results([], [], 5) == []

    def test_first_round_metadata_kept(self):
        # 同一切片两轮都出现时，元数据（如相似度）保留首次出现的轮次
        a = [{**_chunk(1, 1, 0, "same"), "similarity": 0.9}]
        b = [{**_chunk(1, 1, 0, "same"), "similarity": 0.5}]
        assert merge_round_results(a, b, 5)[0]["similarity"] == 0.9
