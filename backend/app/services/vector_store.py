"""Chroma 向量存储公共封装（M10）：统一集合管理与批量读写。

知识库（一库一 collection）与长期记忆（单 collection + user_id 过滤）共用，
消除两处重复的 chroma 初始化 / 批量 upsert / 查询样板。软删等业务过滤仍由调用方
在 MySQL 侧二次执行（Chroma metadata 过滤不可靠的项目经验）。
"""
import logging

import chromadb

from app.core.config import settings

logger = logging.getLogger(__name__)

CLIENT = chromadb.PersistentClient(path=settings.CHROMA_DIR)

DEFAULT_INCLUDE = ["documents", "metadatas", "distances"]


def get_or_create_collection(name: str, dimension: int | None = None):
    """取集合；不存在时创建（cosine 空间，维度在创建时锁定）。"""
    try:
        return CLIENT.get_collection(name)
    except Exception:
        return CLIENT.create_collection(
            name, metadata={"hnsw:space": "cosine", "dimension": dimension or 1024}
        )


def upsert_points(
    col,
    ids: list,
    vectors: list,
    documents: list,
    metadatas: list,
    batch: int = 100,
) -> None:
    """按批量写入/覆盖向量点。"""
    for i in range(0, len(ids), batch):
        col.upsert(
            ids=ids[i : i + batch],
            embeddings=vectors[i : i + batch],
            documents=documents[i : i + batch],
            metadatas=metadatas[i : i + batch],
        )


def delete_ids(col, ids: list) -> None:
    try:
        col.delete(ids=ids)
    except Exception:  # noqa: BLE001 删除失败不阻断业务（MySQL 软删兜底不可见），但必须留痕
        logger.warning("Chroma 向量删除失败 ids=%s", ids, exc_info=True)


def query(col, vector: list, n_results: int, where: dict | None = None):
    """按向量近邻查询，返回 {ids, documents, metadatas, distances} 结构。"""
    return col.query(
        query_embeddings=[vector],
        n_results=n_results,
        where=where,
        include=list(DEFAULT_INCLUDE),
    )
