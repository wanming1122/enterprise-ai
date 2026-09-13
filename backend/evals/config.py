"""评测配置：K 档位、消融配置定义与路径约定。"""
from pathlib import Path

EVALS_DIR = Path(__file__).resolve().parent
DATA_DIR = EVALS_DIR / "data"
REPORTS_DIR = EVALS_DIR / "reports"
SNAPSHOT_PATH = DATA_DIR / "chunks_snapshot.json"
GOLDEN_SET_PATH = DATA_DIR / "golden_set.jsonl"

# 6 为生产默认 top_k；3 看精排头部质量；10 看召回天花板（候选池为 top20）
K_VALUES = (3, 6, 10)

CONFIG_DESC = {
    "b0": "仅向量（Chroma 余弦相似度序）",
    "b1": "仅 BM25（Okapi k1=1.5 b=0.75 + jieba）",
    "b2": "向量+BM25+RRF 融合（重排前融合序）",
    "b3": "B2 + rerank 精排（配置 rerank 后即生产链路）",
    "b4": "B2 + CRAG 改写重检并集合并（需默认 LLM，最多两轮）",
}
