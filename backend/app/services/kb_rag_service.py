"""RAG 入库与检索核心（M3-T2/T3 + M6 二期/三期）：解析切分、向量化、混合检索、重排、CRAG。

- Embedding：模型配置页（ai_model）或 .env 兜底，OpenAI 兼容端点，维度创建库时锁定
- 向量库：Chroma PersistentClient，一库一 collection（kb_{id}），cosine 空间
- 双写：切片文本与元数据写 MySQL kb_chunk，向量与 metadata 写 Chroma
- 检索二期：向量 + BM25（jieba）双路粗排 → RRF 融合 → rerank 模型精排（未配置则按融合序）
- 检索三期：CRAG 简化版——LLM 相关性分级，不足则改写查询重检一轮
"""
import math
import re
import uuid
from datetime import datetime
from pathlib import Path

import chromadb
import httpx
import jieba
import pdfplumber
from docx import Document as DocxDocument
from docx.table import Table as DocxTable
from docx.text.paragraph import Paragraph as DocxParagraph
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import SessionLocal
from app.models.kb import KBChunk, KBFile, KBKnowledgeBase
from app.models.user import SysUser
from app.services import ai_model_service, llm_client
from app.services.operation_log_service import write_log

EMBED_BATCH_SIZE = 16
SEPARATORS = ["\n\n", "\n", "。", "；", "，", " "]
CHROMA_CLIENT = chromadb.PersistentClient(path=settings.CHROMA_DIR)


# ---------- Embedding ----------

def _embed(texts: list[str], dimensions: int, config: dict) -> list[list[float]]:
    """调用 OpenAI 兼容 embeddings 端点（配置来自模型配置页或 .env 兜底），批量分片请求。"""
    if not config.get("api_key"):
        raise HTTPException(status_code=422, detail="未配置向量模型 API 密钥")
    vectors: list[list[float]] = []
    with httpx.Client(timeout=60) as client:
        for i in range(0, len(texts), EMBED_BATCH_SIZE):
            batch = texts[i:i + EMBED_BATCH_SIZE]
            for attempt in range(2):  # 失败重试一次
                r = client.post(
                    f"{config['base_url']}/embeddings",
                    headers={"Authorization": f"Bearer {config['api_key']}"},
                    json={"model": config["model_name"], "input": batch, "dimensions": dimensions},
                )
                if r.status_code == 200:
                    data = sorted(r.json()["data"], key=lambda x: x["index"])
                    vectors.extend(item["embedding"] for item in data)
                    break
                if attempt == 1:
                    raise HTTPException(status_code=422, detail=f"向量化失败：{r.text[:200]}")
    return vectors


def probe_embedding_dimension(config: dict | None = None) -> int:
    """创建知识库时探测向量维度（探针一次，写入 kb 表后锁定）。"""
    config = config or ai_model_service.resolve_embedding_config()
    return len(_embed(["维度探针"], 1024, config)[0])


def embed_texts(texts: list[str], dimensions: int, *, model_name: str | None = None,
                db: Session | None = None) -> list[list[float]]:
    """按模型配置向量化：优先匹配 model_name 的启用配置，其次类型默认，再退 .env。"""
    config = ai_model_service.resolve_embedding_config(db=db, model_name=model_name)
    return _embed(texts, dimensions, config)


# ---------- 解析与切分 ----------

def _clean(text: str) -> str:
    """清洗：规整空白与多余空行。"""
    text = text.replace("\x00", "")
    lines = [ln.rstrip() for ln in text.splitlines()]
    out: list[str] = []
    blank = 0
    for ln in lines:
        if not ln.strip():
            blank += 1
            if blank <= 1:
                out.append("")
        else:
            blank = 0
            out.append(ln)
    return "\n".join(out).strip()


def _split_text(text: str, size: int, overlap: int) -> list[str]:
    """递归字符切分：按分隔符优先级归组，块间携带 overlap 字符。"""
    text = text.strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]

    segments: list[str] = []
    for sep in SEPARATORS:
        if sep and sep in text:
            segments = [p for p in text.split(sep) if p.strip()]
            if len(segments) > 1:
                # 二次细分超长段
                flat: list[str] = []
                for seg in segments:
                    flat.extend(_split_text(seg, size, 0) if len(seg) > size else [seg])
                break
    if not segments:
        segments = [text[i:i + size] for i in range(0, len(text), size - overlap)]  # 硬切兜底

    chunks: list[str] = []
    current = ""
    for seg in segments:
        candidate = f"{current}{sep if current else ''}{seg}" if current else seg
        # 兼容 sep 已并入 segments 拆分的场景：手动补分隔符
        if len(candidate) <= size:
            current = candidate
        else:
            if current:
                chunks.append(current)
            tail = current[-overlap:] if current and overlap > 0 else ""
            current = (tail + seg)[:size] if tail else seg[:size]
            while len(seg) > size:  # 单段超长硬切
                chunks.append(seg[:size])
                seg = seg[size - overlap:]
                current = seg
    if current:
        chunks.append(current)
    return [c.strip() for c in chunks if c.strip()]


def _table_to_markdown(rows: list[list[str | None]]) -> str:
    """表格转 Markdown，空单元格以空串占位。"""
    rows = [[("" if c is None else str(c).strip().replace("\n", " ")) for c in row] for row in rows]
    if not rows:
        return ""
    header = rows[0]
    lines = ["| " + " | ".join(header) + " |", "| " + " | ".join(["---"] * len(header)) + " |"]
    for row in rows[1:]:
        row = row + [""] * (len(header) - len(row))
        lines.append("| " + " | ".join(row[: len(header)]) + " |")
    return "\n".join(lines)


def _split_table(rows: list[list[str | None]], size: int) -> list[str]:
    """大表按行分组切分，每组重复表头。"""
    md = _table_to_markdown(rows)
    if len(md) <= size or len(rows) <= 2:
        return [md] if md.strip() else []
    header, body = rows[0], rows[1:]
    group_size = max(5, len(body) * size // max(1, len(md)) or 5)
    chunks = []
    for i in range(0, len(body), group_size):
        part = _table_to_markdown([header, *body[i:i + group_size]])
        if part.strip():
            chunks.append(part)
    return chunks


def _parse_pdf(path: Path) -> list[dict]:
    """PDF：逐页文本 + 表格，携带页码。"""
    items: list[dict] = []
    with pdfplumber.open(path) as pdf:
        for page_no, page in enumerate(pdf.pages, start=1):
            for table in page.extract_tables():
                for part in _split_table(table, 500):
                    items.append({"content": part, "title_path": None, "page": page_no, "chunk_type": "table"})
            text = page.extract_text() or ""
            if text.strip():
                items.append({"content": _clean(text), "title_path": None, "page": page_no, "chunk_type": "text"})
    return items


def _parse_docx(path: Path) -> list[dict]:
    """Word：按标题样式维护标题路径，段落成文、表格转 Markdown。"""
    items: list[dict] = []
    doc = DocxDocument(str(path))
    title_stack: list[str] = []

    def add_text(text: str) -> None:
        if text.strip():
            items.append({"content": _clean(text), "title_path": ">".join(title_stack) or None,
                          "page": None, "chunk_type": "text"})

    buffer: list[str] = []
    for element in doc.element.body:
        tag = element.tag.rsplit("}", 1)[-1]
        if tag == "p":
            para = DocxParagraph(element, doc)
            style = (para.style.name or "") if para.style is not None else ""
            if style.startswith("Heading") or style.startswith("标题"):
                if buffer:
                    add_text("\n".join(buffer))
                    buffer = []
                level = "".join(ch for ch in style if ch.isdigit()) or "1"
                level = int(level)
                title_stack[:] = title_stack[: level - 1]
                title_stack.append(para.text.strip())
            elif para.text.strip():
                buffer.append(para.text)
        elif tag == "tbl":
            if buffer:
                add_text("\n".join(buffer))
                buffer = []
            table = DocxTable(element, doc)
            rows = [[c.text for c in row.cells] for row in table.rows]
            for part in _split_table(rows, 500):
                items.append({"content": part, "title_path": ">".join(title_stack) or None,
                              "page": None, "chunk_type": "table"})
    if buffer:
        add_text("\n".join(buffer))
    return items


def _parse_markdown(text: str) -> list[dict]:
    """Markdown：按标题层级切分并记录标题路径。"""
    items: list[dict] = []
    title_stack: list[str] = []
    section: list[str] = []
    for line in text.splitlines():
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            if section:
                items.append({"content": _clean("\n".join(section)),
                              "title_path": ">".join(title_stack) or None,
                              "page": None, "chunk_type": "text"})
                section = []
            level = len(m.group(1))
            title_stack[:] = title_stack[: level - 1]
            title_stack.append(m.group(2).strip())
        else:
            section.append(line)
    if section:
        items.append({"content": _clean("\n".join(section)),
                      "title_path": ">".join(title_stack) or None,
                      "page": None, "chunk_type": "text"})
    return items


def parse_file_to_items(file_type: str, path: Path) -> list[dict]:
    """按类型解析文件为内容片段列表（未切分）。"""
    if file_type == "pdf":
        return _parse_pdf(path)
    if file_type == "docx":
        return _parse_docx(path)
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("gbk", errors="ignore")
    if file_type == "md":
        return _parse_markdown(text)
    return [{"content": _clean(text), "title_path": None, "page": None, "chunk_type": "text"}]


def build_chunks(kb: KBKnowledgeBase, file_id: int, items: list[dict]) -> list[dict]:
    """将解析片段切分为最终切片（表格整块直通，文本走递归切分）。"""
    chunks: list[dict] = []
    index = 0
    for item in items:
        if item["chunk_type"] == "table":
            parts = [item["content"]] if item["content"] else []
        else:
            parts = _split_text(item["content"], kb.chunk_size, kb.chunk_overlap)
        for part in parts:
            chunks.append({
                "file_id": file_id, "kb_id": kb.id, "chunk_index": index,
                "content": part, "char_count": len(part),
                "title_path": item["title_path"], "page": item["page"],
                "chunk_type": item["chunk_type"], "status": 1,
            })
            index += 1
    return chunks


# ---------- Chroma ----------

def _get_collection(kb: KBKnowledgeBase, create: bool = True):
    try:
        return CHROMA_CLIENT.get_collection(kb.collection_name)
    except Exception:
        if not create:
            return None
        return CHROMA_CLIENT.create_collection(
            kb.collection_name, metadata={"hnsw:space": "cosine", "dimension": kb.embedding_dimension}
        )


def upsert_vectors(kb: KBKnowledgeBase, chunks: list[dict]) -> None:
    """切片向量写入 Chroma（覆盖同 ID），metadata 带溯源与软删标记。"""
    if not chunks:
        return
    collection = _get_collection(kb)
    vectors = embed_texts([c["content"] for c in chunks], kb.embedding_dimension, model_name=kb.embedding_model)
    ids = [f"f{c['file_id']}c{c['chunk_index']}" for c in chunks]
    documents = [c["content"] for c in chunks]
    metadatas = [
        {
            "kb_id": c["kb_id"], "file_id": c["file_id"], "chunk_index": c["chunk_index"],
            "title_path": c["title_path"] or "", "page": c["page"] or 0,
            "chunk_type": c["chunk_type"], "status": "active",
        }
        for c in chunks
    ]
    for i in range(0, len(ids), 100):
        collection.upsert(
            ids=ids[i:i + 100], embeddings=vectors[i:i + 100],
            documents=documents[i:i + 100], metadatas=metadatas[i:i + 100],
        )


def remove_file_vectors(kb: KBKnowledgeBase, file_id: int) -> None:
    """文件软删时同步移除其向量（collection 不存在则忽略）。"""
    collection = _get_collection(kb, create=False)
    if collection is not None:
        collection.delete(where={"file_id": file_id})


# ---------- 检索（二期混合检索 + 重排；三期 CRAG） ----------

HYBRID_CANDIDATES = 20   # 粗排：向量与 BM25 各取候选数（设计稿：粗排 top 20）
RRF_K = 60               # RRF 融合常数（业界常用 60）
RERANK_MIN_SCORE = 0.2   # 重排相关性分阈值，低于丢弃（宁少勿滥）
BM25_K1 = 1.5            # BM25 词频饱和参数
BM25_B = 0.75            # BM25 长度归一化参数
BM25_CORPUS_CAP = 5000   # BM25 语料切片上限（演示规模内全量，超出按 id 截断）
_bm25_cache: dict = {}   # BM25 语料缓存：{frozenset(kb_ids): {"size": n, "max_id": m, "docs": [...]}}


def _tokenize(text: str) -> list[str]:
    """检索分词：jieba 中文切词 + 小写化；保留长度≥2的词与纯字母数字短词。"""
    out: list[str] = []
    for tok in jieba.lcut(text.lower()):
        tok = tok.strip()
        if not tok:
            continue
        if re.fullmatch(r"[a-z0-9@.]+", tok) or len(tok) >= 2:
            out.append(tok)
    return out


def _bm25_corpus(db: Session, kb_ids: list[int]) -> list[dict]:
    """BM25 语料：有效知识库的有效切片（MySQL 侧），按切片总量与新切片 id 变化失效缓存。"""
    key = frozenset(kb_ids)
    row = db.execute(
        select(func.count(), func.coalesce(func.max(KBChunk.id), 0))
        .join(KBFile, KBFile.id == KBChunk.file_id)
        .where(KBChunk.kb_id.in_(kb_ids), KBChunk.status != 2, KBFile.status != 2)
    ).one()
    total, max_id = int(row[0] or 0), int(row[1] or 0)
    cached = _bm25_cache.get(key)
    if cached and cached["size"] == total and cached["max_id"] == max_id:
        return cached["docs"]
    chunks = db.scalars(
        select(KBChunk).join(KBFile, KBFile.id == KBChunk.file_id)
        .where(KBChunk.kb_id.in_(kb_ids), KBChunk.status != 2, KBFile.status != 2)
        .order_by(KBChunk.id).limit(BM25_CORPUS_CAP)
    ).all()
    docs = [
        {
            "kb_id": c.kb_id, "file_id": c.file_id, "chunk_index": c.chunk_index,
            "content": c.content, "title_path": c.title_path, "page": c.page,
            "chunk_type": c.chunk_type, "tokens": _tokenize(c.content),
        }
        for c in chunks
    ]
    _bm25_cache[key] = {"size": total, "max_id": max_id, "docs": docs}
    return docs


def _bm25_search(query_tokens: list[str], docs: list[dict], top_n: int) -> list[dict]:
    """经典 BM25（Okapi）打分，返回前 top_n 候选（带 bm25_score）。"""
    if not query_tokens or not docs:
        return []
    avgdl = sum(len(d["tokens"]) for d in docs) / len(docs) or 1.0
    df: dict[str, int] = {}
    for d in docs:
        for t in set(d["tokens"]):
            df[t] = df.get(t, 0) + 1
    scored: list[tuple[float, dict]] = []
    for d in docs:
        tf: dict[str, int] = {}
        for t in d["tokens"]:
            tf[t] = tf.get(t, 0) + 1
        dl = len(d["tokens"]) or 1
        score = 0.0
        for t in query_tokens:
            freq = tf.get(t)
            if not freq or t not in df:
                continue
            idf = math.log(1 + (len(docs) - df[t] + 0.5) / (df[t] + 0.5))
            score += idf * freq * (BM25_K1 + 1) / (freq + BM25_K1 * (1 - BM25_B + BM25_B * dl / avgdl))
        if score > 0:
            scored.append((score, d))
    scored.sort(key=lambda x: -x[0])
    return [
        {
            "kb_id": d["kb_id"], "file_id": d["file_id"], "chunk_index": d["chunk_index"],
            "content": d["content"], "title_path": d["title_path"], "page": d["page"],
            "chunk_type": d["chunk_type"], "bm25_score": round(score, 4),
        }
        for score, d in scored[:top_n]
    ]


def _rrf_merge(vector_results: list[dict], bm25_results: list[dict], top_n: int) -> list[dict]:
    """Reciprocal Rank Fusion：按 (kb_id, file_id, chunk_index) 归并双路排名。

    向量路各库独立 collection，相似度跨库不可比，名次取库内名次（vec_rank）；
    BM25 语料为多库合并的全量切片，名次即全局名次。
    """
    def key(r: dict) -> tuple:
        return (r["kb_id"], r["file_id"], r["chunk_index"])

    fused: dict[tuple, dict] = {}
    for i, r in enumerate(vector_results, start=1):
        rank = r.get("vec_rank") or i
        entry = fused.setdefault(key(r), {**r, "rrf": 0.0})
        entry["rrf"] += 1.0 / (RRF_K + rank)
    for rank, r in enumerate(bm25_results, start=1):
        entry = fused.get(key(r))
        if entry is not None:
            entry["rrf"] += 1.0 / (RRF_K + rank)
        else:
            fused[key(r)] = {**r, "similarity": None, "rrf": 1.0 / (RRF_K + rank)}
    return sorted(fused.values(), key=lambda x: -x["rrf"])[:top_n]


def _rerank(db: Session, query: str, candidates: list[dict]) -> list[dict] | None:
    """重排精排：调用默认 rerank 模型的 /rerank 端点（Cohere/Jina 风格）。

    未配置重排模型或调用失败返回 None，调用方按融合序降级（不阻塞检索）。
    """
    cfg = ai_model_service.resolve_rerank_config(db=db)
    if not cfg or not cfg.get("api_key"):
        return None
    try:
        with httpx.Client(timeout=30) as client:
            r = client.post(
                f"{cfg['base_url']}/rerank",
                headers={"Authorization": f"Bearer {cfg['api_key']}"},
                json={
                    "model": cfg["model_name"], "query": query,
                    "documents": [c["content"][:2000] for c in candidates],
                    "top_n": len(candidates),
                },
            )
        if r.status_code != 200:
            return None
        ranked: list[dict] = []
        for item in sorted(r.json().get("results", []), key=lambda x: -x.get("relevance_score", 0)):
            idx = item.get("index")
            if isinstance(idx, int) and 0 <= idx < len(candidates):
                ranked.append({**candidates[idx], "similarity": round(float(item.get("relevance_score", 0)), 4)})
        return ranked
    except Exception:
        return None


def _query_vectors_for_collections(db: Session, query: str, members: list[tuple]) -> list[tuple]:
    """按（embedding 模型, 维度）分组嵌入查询向量：同组各库复用一次 embedding 调用。

    入参 members 为 (知识库, Chroma collection) 列表；返回 (知识库, collection, 查询向量)。
    全部知识库共用同一模型时，多库检索的查询嵌入从逐库一次收敛为整批一次。
    """
    groups: dict[tuple, list] = {}
    for kb, collection in members:
        groups.setdefault((kb.embedding_model, kb.embedding_dimension), []).append((kb, collection))
    out: list[tuple] = []
    for (model_name, dimension), items in groups.items():
        qv = embed_texts([query], dimension, model_name=model_name, db=db)[0]
        for kb, collection in items:
            out.append((kb, collection, qv))
    return out


def retrieve_with_stages(db: Session, *, query: str, kb_ids: list[int], top_k: int = 6) -> dict:
    """阶段化混合检索：向量/BM25 双路粗排 → RRF 融合 → 重排精排，返回各阶段完整候选。

    供评测与检索调试观测各阶段表现；生产入口 retrieve() 委托本函数，保证单一实现。
    返回字段：
    - vector：软删过滤后、进入融合前的向量路候选（各库内 vec_rank 排列）
    - bm25：BM25 粗排候选（bm25_score 降序）
    - fused：RRF 融合候选（重排前的融合序）
    - reranked：重排模型原始输出（未配置重排模型时为 None）
    - final：软删兜底过滤并截断 top_k 后的最终结果
    similarity 字段语义：重排后为 rerank 相关性分；未重排时向量路为余弦相似度，BM25 独有候选为 None。
    """
    kbs = db.scalars(
        select(KBKnowledgeBase).where(KBKnowledgeBase.id.in_(kb_ids), KBKnowledgeBase.status != 2)
    ).all()
    if not kbs:
        raise HTTPException(status_code=422, detail="知识库不存在或已删除")

    # 1) 向量粗排：按（embedding 模型, 维度）分组嵌入查询向量，组内各库复用
    members: list[tuple] = []
    for kb in kbs:
        collection = _get_collection(kb, create=False)
        if collection is not None:
            members.append((kb, collection))
    vector_results: list[dict] = []
    for kb, collection, qv in _query_vectors_for_collections(db, query, members):
        res = collection.query(
            query_embeddings=[qv], n_results=HYBRID_CANDIDATES,
            where={"status": "active"},
            include=["documents", "metadatas", "distances"],
        )
        for vec_rank, (doc, meta, dist) in enumerate(
            zip(res["documents"][0], res["metadatas"][0], res["distances"][0]), start=1
        ):
            vector_results.append({
                "kb_id": kb.id, "kb_name": kb.name,
                "file_id": meta.get("file_id"), "chunk_index": meta.get("chunk_index"),
                "content": doc,
                "title_path": meta.get("title_path") or None,
                "page": meta.get("page") or None,
                "chunk_type": meta.get("chunk_type", "text"),
                "similarity": round(max(0.0, 1.0 - dist), 4),  # cosine 距离转相似度
                "vec_rank": vec_rank,  # 库内名次，供 RRF 使用（跨库相似度不可比）
            })

    # 2) MySQL 侧兜底过滤（文件软删即不可引用）+ BM25 粗排
    file_ids = {r["file_id"] for r in vector_results if r["file_id"]}
    valid = set(
        db.scalars(select(KBFile.id).where(KBFile.id.in_(file_ids), KBFile.status != 2)).all()
    ) if file_ids else set()
    vector_results = [r for r in vector_results if r["file_id"] in valid]
    bm25_results = _bm25_search(_tokenize(query), _bm25_corpus(db, kb_ids), HYBRID_CANDIDATES)

    # 3) RRF 融合 → 重排精排
    fused = _rrf_merge(vector_results, bm25_results, HYBRID_CANDIDATES)
    ranked = _rerank(db, query, fused)
    candidates = fused
    if ranked is not None:
        candidates = [r for r in ranked if r["similarity"] >= RERANK_MIN_SCORE]

    # 4) 补齐名称与软删过滤（覆盖 BM25 独有候选）
    kb_names = {kb.id: kb.name for kb in kbs}
    cand_file_ids = {r["file_id"] for r in candidates if r["file_id"]}
    valid_files = set(
        db.scalars(select(KBFile.id).where(KBFile.id.in_(cand_file_ids), KBFile.status != 2)).all()
    ) if cand_file_ids else set()
    file_names = dict(
        db.execute(select(KBFile.id, KBFile.file_name).where(KBFile.id.in_(cand_file_ids))).all()
    ) if cand_file_ids else {}
    results: list[dict] = []
    for r in candidates:
        if r["file_id"] not in valid_files:
            continue
        r["kb_name"] = kb_names.get(r["kb_id"], "")
        r["file_name"] = file_names.get(r["file_id"], "")
        results.append(r)

    return {
        "vector": vector_results,
        "bm25": bm25_results,
        "fused": fused,
        "reranked": ranked,
        "final": results[:top_k],
    }


def retrieve(db: Session, *, query: str, kb_ids: list[int], top_k: int = 6) -> list[dict]:
    """混合检索（生产入口）：向量 + BM25 双路粗排 → RRF 融合 → 重排精排（未配置重排模型时按融合序取）。

    委托 retrieve_with_stages，仅返回最终 top_k 结果。
    """
    return retrieve_with_stages(db, query=query, kb_ids=kb_ids, top_k=top_k)["final"]


GRADE_PROMPT = (
    "判断以下检索资料能否回答用户问题。只回答两个词之一：充分 或 不足。\n\n"
    "用户问题：{q}\n\n检索资料：\n{ctx}"
)
REWRITE_FOR_RETRIEVE_PROMPT = (
    "以下检索资料与用户问题不匹配，请生成一个新的检索查询（不超过30字），"
    "调整措辞与关键词以便命中相关资料。只输出查询本身。\n\n用户问题：{q}\n\n已检索资料摘要：\n{ctx}"
)


def _grade_sufficient(query: str, results: list[dict]) -> bool:
    """LLM 相关性分级：资料足以回答返回 True；模型异常时放行，不阻塞问答。"""
    if not results:
        return False
    ctx = "\n".join(f"[{i + 1}] {r['content'][:200]}" for i, r in enumerate(results))
    try:
        answer = llm_client.chat_once(
            [{"role": "user", "content": GRADE_PROMPT.format(q=query, ctx=ctx[:2400])}],
            max_tokens=8, temperature=0,
        )
    except HTTPException:
        return True
    return "充分" in answer and "不足" not in answer


def _rewrite_retrieval_query(query: str, results: list[dict]) -> str | None:
    """CRAG 检索校正：基于未命中的资料摘要改写查询；失败返回 None。"""
    ctx = "\n".join(f"[{i + 1}] {r['content'][:120]}" for i, r in enumerate(results))
    try:
        rewritten = llm_client.chat_once(
            [{"role": "user", "content": REWRITE_FOR_RETRIEVE_PROMPT.format(q=query, ctx=ctx[:1500])}],
            max_tokens=64, temperature=0.2,
        )
    except HTTPException:
        return None
    rewritten = rewritten.strip().strip('"“”').replace("\n", " ")
    return rewritten[:60] or None


def merge_round_results(round_a: list[dict], round_b: list[dict], top_n: int,
                        weight_a: float = 2.0, weight_b: float = 1.0) -> list[dict]:
    """跨检索轮次的加权 RRF 并集合并（CRAG 改写轮防漂移）。

    改写查询重检的结果若直接替换上一轮，会把已命中的相关切片挤出结果（评测 q017 案例）；
    对称融合也不行——改写轮查询质量不可靠，其名次分会把原查询中排名靠后的 gold 挤出
    top_k（v3 评测复现）。故原查询轮（round_a，代表用户真实意图）加权 weight_a，
    改写轮（round_b）加权 weight_b：改写轮只在原查询头部之外补充填充，不会挤占既有命中。
    按 (kb_id, file_id, chunk_index) 去重，weight/(RRF_K+名次) 累加排序再截断；
    元数据保留首次出现的轮次。纯函数，便于测试。
    """
    def key(r: dict) -> tuple:
        return (r["kb_id"], r["file_id"], r["chunk_index"])

    fused: dict[tuple, dict] = {}
    for results, weight in ((round_a, weight_a), (round_b, weight_b)):
        for rank, r in enumerate(results, start=1):
            entry = fused.setdefault(key(r), {**r, "round_rrf": 0.0})
            entry["round_rrf"] += weight / (RRF_K + rank)
    merged = sorted(fused.values(), key=lambda x: -x["round_rrf"])
    return merged[:top_n]


def retrieve_with_crag(db: Session, *, query: str, kb_ids: list[int], top_k: int = 6,
                       max_rounds: int = 2) -> tuple[list[dict], str]:
    """CRAG 简化版（RAG 三期）：检索 → LLM 分级 → 不足则改写查询重检，最多 max_rounds 轮。

    重检结果与上一轮加权融合取并集（原查询轮权重更高，而非替换），改写查询漂移时
    已命中的相关切片不会被挤出，改写轮仅在余量内补充。返回 (最终结果, 实际使用的检索查询)；
    分级或重写失败时静默保留上一轮结果。
    """
    used_query = query
    results = retrieve(db, query=used_query, kb_ids=kb_ids, top_k=top_k)
    for _ in range(max_rounds - 1):
        if _grade_sufficient(used_query, results):
            break
        rewritten = _rewrite_retrieval_query(used_query, results)
        if not rewritten or rewritten == used_query:
            break
        used_query = rewritten
        new_results = retrieve(db, query=used_query, kb_ids=kb_ids, top_k=top_k)
        results = merge_round_results(results, new_results, top_k)
    return results, used_query


def drop_kb_collection(kb: KBKnowledgeBase) -> None:
    """整库删除时移除 collection。"""
    try:
        CHROMA_CLIENT.delete_collection(kb.collection_name)
    except Exception:
        pass


def rebuild_kb_vectors(db: Session, kb: KBKnowledgeBase, operator: SysUser | None = None) -> dict:
    """按库重建向量索引：删除并重建 collection 后从 kb_chunk 全量重嵌入。"""
    drop_kb_collection(kb)
    chunks = db.scalars(
        select(KBChunk).where(KBChunk.kb_id == kb.id, KBChunk.status != 2).order_by(KBChunk.file_id, KBChunk.chunk_index)
    ).all()
    data = [
        {"file_id": c.file_id, "kb_id": c.kb_id, "chunk_index": c.chunk_index, "content": c.content,
         "title_path": c.title_path, "page": c.page, "chunk_type": c.chunk_type}
        for c in chunks
    ]
    for i in range(0, len(data), 256):
        upsert_vectors(kb, data[i:i + 256])
    write_log(db, user_id=operator.id if operator else None,
              username=operator.username if operator else "system", module="知识库",
              action="重建索引", params={"kb_id": kb.id, "chunks": len(data)}, result=1)
    return {"kb_id": kb.id, "chunks": len(data)}


# ---------- 异步入库任务 ----------

def process_file(file_id: int) -> None:
    """后台任务：待解析文件 → 解析 → 切分 → 向量化 → 双写。使用独立数据库会话。"""
    db = SessionLocal()
    try:
        kb_file = db.get(KBFile, file_id)
        if kb_file is None or kb_file.status == 2 or kb_file.parse_status not in (0, 3):
            return
        kb = db.get(KBKnowledgeBase, kb_file.kb_id)
        if kb is None or kb.status == 2:
            return

        kb_file.parse_status = 1
        kb_file.fail_reason = None
        db.commit()

        try:
            path = Path(settings.MEDIA_DIR) / kb_file.storage_path
            items = parse_file_to_items(kb_file.file_type, path)
            chunks = build_chunks(kb, kb_file.id, items)

            # 清空旧切片（重解析场景）
            for old in db.scalars(select(KBChunk).where(KBChunk.file_id == kb_file.id)).all():
                db.delete(old)
            db.flush()
            # 同步清空该文件全部旧向量：新切片数可能变少，残留向量会以过期内容被检索命中
            remove_file_vectors(kb, kb_file.id)
            db.add_all([KBChunk(created_at=datetime.now(), **c) for c in chunks])
            kb_file.chunk_count = len(chunks)

            upsert_vectors(kb, chunks)
            kb_file.parse_status = 2
            db.commit()
        except Exception as exc:  # 记录失败原因，可重试
            db.rollback()
            kb_file = db.get(KBFile, file_id)
            kb_file.parse_status = 3
            kb_file.fail_reason = str(exc)[:250]
            db.commit()
    finally:
        db.close()
