"""导出有效语料快照：有效库的有效文件的有效切片，作为金标标注的事实依据与评测校验基准。

快照即标注时点的语料唯一事实来源：金标集引用的 (kb_id, file_id, chunk_index) 必须
存在于快照中；语料变更后需重新导出快照并复核金标标注。

用法（backend 目录下）：python -m evals.dump_snapshot
"""
import json
from datetime import datetime

from sqlalchemy import select

from app.db.session import SessionLocal
from app.models.kb import KBChunk, KBFile, KBKnowledgeBase

from evals.config import SNAPSHOT_PATH


def dump() -> dict:
    db = SessionLocal()
    try:
        kbs = db.scalars(
            select(KBKnowledgeBase).where(KBKnowledgeBase.status != 2).order_by(KBKnowledgeBase.id)
        ).all()
        files = db.scalars(select(KBFile).where(KBFile.status != 2).order_by(KBFile.id)).all()
        chunks = db.scalars(select(KBChunk).where(KBChunk.status != 2).order_by(KBChunk.id)).all()
        kb_name = {kb.id: kb.name for kb in kbs}
        file_name = {f.id: f.file_name for f in files}
        active_kb_ids = {kb.id for kb in kbs}
        active_file_ids = {f.id for f in files}
        data = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "kbs": [
                {"id": kb.id, "name": kb.name, "chunk_size": kb.chunk_size,
                 "chunk_overlap": kb.chunk_overlap, "embedding_dimension": kb.embedding_dimension}
                for kb in kbs
            ],
            "files": [
                {"id": f.id, "kb_id": f.kb_id, "kb_name": kb_name.get(f.kb_id, ""),
                 "file_name": f.file_name, "file_type": f.file_type, "chunk_count": f.chunk_count}
                for f in files if f.kb_id in active_kb_ids
            ],
            "chunks": [
                {"kb_id": c.kb_id, "file_id": c.file_id, "chunk_index": c.chunk_index,
                 "kb_name": kb_name.get(c.kb_id, ""), "file_name": file_name.get(c.file_id, ""),
                 "title_path": c.title_path, "page": c.page, "chunk_type": c.chunk_type,
                 "char_count": c.char_count, "content": c.content}
                for c in chunks
                if c.kb_id in active_kb_ids and c.file_id in active_file_ids
            ],
        }
        SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"快照已写入 {SNAPSHOT_PATH}")
        print(f"有效库 {len(data['kbs'])} 个 / 有效文件 {len(data['files'])} 个 / 有效切片 {len(data['chunks'])} 个")
        return data
    finally:
        db.close()


if __name__ == "__main__":
    dump()
