"""RAG 语料种子脚本：批量创建内部知识库并走正式入库链路（解析→切分→向量化→MySQL/Chroma 双写）。

用法（在 backend 目录下执行）：
    .venv/Scripts/python.exe scripts/seed_rag_kbs.py

说明：
- 语料位于 scripts/rag_seed_docs/<知识库名>/*.md，目录名即知识库名；
- 脚本幂等：同名启用知识库直接复用，同内容文件（SHA-256 相同）自动跳过；
- 走 kb_service/kb_rag_service 的服务层代码，与页面上传入库行为完全一致。
"""
import io
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

DOCS_DIR = Path(__file__).resolve().parent / "rag_seed_docs"

KB_SPECS = [
    {"name": "行政办公管理制度库", "description": "会议室预订、办公用品与固定资产、门禁工卡与车辆管理等行政办公制度。"},
    {"name": "IT服务与信息安全库", "description": "账号密码与VPN安全策略、IT服务台工单SLA、数据备份与安全应急响应预案。"},
    {"name": "客户服务与售后支持库", "description": "客户工单分级与响应SLA、退换货与退款流程、投诉处理与升级机制。"},
    {"name": "法务合规与合同管理库", "description": "合同审批与用印管理、保密协议与商业秘密、数据合规与个人信息保护。"},
    {"name": "采购与供应商管理库", "description": "采购方式分级与审批权限、供应商准入资质与考核分级制度。"},
]


def main() -> int:
    from fastapi import UploadFile
    from sqlalchemy import select

    from app.db.session import SessionLocal
    from app.models.kb import KBKnowledgeBase
    from app.models.user import SysUser
    from app.schemas.kb import KBCreate
    from app.services import kb_rag_service
    from app.services.kb_service import create_kb, upload_file

    db = SessionLocal()
    admin = db.scalar(select(SysUser).where(SysUser.username == "admin"))
    if admin is None:
        print("[错误] 未找到 admin 用户，请先初始化系统。")
        return 1

    total_files = 0
    for spec in KB_SPECS:
        kb = db.scalar(
            select(KBKnowledgeBase).where(KBKnowledgeBase.name == spec["name"], KBKnowledgeBase.status != 2)
        )
        if kb is None:
            created = create_kb(
                db,
                KBCreate(name=spec["name"], description=spec["description"],
                         chunk_size=500, chunk_overlap=80, embedding_dimension=1024),
                admin,
            )
            kb = db.get(KBKnowledgeBase, created["id"])
            print(f"[建库] {spec['name']} -> kb_id={kb.id} (embedding={kb.embedding_model}, dim={kb.embedding_dimension})")
        else:
            print(f"[复用] {spec['name']} 已存在 kb_id={kb.id}")

        docs = sorted((DOCS_DIR / spec["name"]).glob("*.md"))
        if not docs:
            print(f"[警告] {spec['name']} 语料目录为空：{DOCS_DIR / spec['name']}")
            continue
        for doc in docs:
            content = doc.read_bytes()
            upload = UploadFile(file=io.BytesIO(content), filename=doc.name)
            try:
                f = upload_file(db, kb, upload, admin)
            except Exception as exc:  # 同内容重复上传等场景：跳过
                print(f"[跳过] {doc.name}：{getattr(exc, 'detail', exc)}")
                continue
            kb_rag_service.process_file(f["id"])
            total_files += 1
            print(f"[入库] kb={spec['name']} file={doc.name} file_id={f['id']}")
    db.close()
    print(f"\n完成：本轮实际入库文件 {total_files} 个。解析与向量化均同步执行完毕。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
