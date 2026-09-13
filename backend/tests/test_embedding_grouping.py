"""检索阶段查询向量分组嵌入测试：同（embedding 模型, 维度）组只调一次 embedding。

monkeypatch embed_texts 与 Chroma collection，不触真实向量库与外部 API。
"""
from types import SimpleNamespace

from app.services import kb_rag_service


def _kb(kb_id: int, model_name: str, dimension: int):
    return SimpleNamespace(id=kb_id, embedding_model=model_name, embedding_dimension=dimension)


class TestQueryVectorsForCollections:
    def test_same_model_group_single_embed_call(self, monkeypatch):
        calls = []

        def fake_embed(texts, dimensions, *, model_name=None, db=None):
            calls.append((model_name, dimensions, list(texts)))
            return [[0.1] * dimensions]

        monkeypatch.setattr(kb_rag_service, "embed_texts", fake_embed)
        members = [
            (_kb(1, "embedding-3", 1024), object()),
            (_kb(2, "embedding-3", 1024), object()),
            (_kb(3, "other-model", 512), object()),
        ]
        out = kb_rag_service._query_vectors_for_collections(db=None, query="测试问题", members=members)

        # 3 个库、2 个分组：同模型同维度的库 1/2 共用一次调用
        assert len(calls) == 2
        assert calls[0] == ("embedding-3", 1024, ["测试问题"])
        assert calls[1] == ("other-model", 512, ["测试问题"])
        assert len(out) == 3
        vec_a = next(v for kb, _, v in out if kb.id == 1)
        vec_b = next(v for kb, _, v in out if kb.id == 2)
        assert vec_a is vec_b  # 同组复用同一查询向量

    def test_each_group_embeds_query_once(self, monkeypatch):
        monkeypatch.setattr(
            kb_rag_service, "embed_texts",
            lambda texts, dimensions, *, model_name=None, db=None: [[0.2] * dimensions],
        )
        members = [(_kb(i, "embedding-3", 1024), object()) for i in range(1, 11)]
        out = kb_rag_service._query_vectors_for_collections(db=None, query="q", members=members)
        assert len(out) == 10  # 10 库同模型：1 次调用覆盖全部
