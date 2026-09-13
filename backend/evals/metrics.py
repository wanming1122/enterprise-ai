"""IR 评测指标（纯 stdlib）：Recall / Precision / HitRate / MRR / NDCG。

约定：
- retrieved：按排名先后排列的候选键列表，键 = (kb_id, file_id, chunk_index)
- relevant：{键: 相关度等级}，等级 >= 1 视为相关；2=核心答案切片，1=辅助/部分相关
- relevant 为空（负例）时一律返回 None，由调用方剔除后不计入均值
"""
import math


def _hits(retrieved: list, relevant: dict, k: int) -> list:
    return [doc for doc in retrieved[:k] if doc in relevant]


def recall_at_k(retrieved: list, relevant: dict, k: int) -> float | None:
    """命中相关切片数 / 全部相关切片数：RAG 场景主指标（相关切片进入上下文即可被引用）。"""
    if not relevant:
        return None
    return len(_hits(retrieved, relevant, k)) / len(relevant)


def precision_at_k(retrieved: list, relevant: dict, k: int) -> float | None:
    """topK 中相关切片占比：反映上下文槽位被无关内容浪费的程度。"""
    if not relevant:
        return None
    return len(_hits(retrieved, relevant, k)) / k


def hit_rate_at_k(retrieved: list, relevant: dict, k: int) -> float | None:
    """至少命中一个相关切片记 1 分：粗粒度「是否完全脱靶」。"""
    if not relevant:
        return None
    return 1.0 if _hits(retrieved, relevant, k) else 0.0


def mrr_at_k(retrieved: list, relevant: dict, k: int) -> float | None:
    """第一个相关切片排名倒数的均值：单答案事实型问题对榜首位置极敏感。"""
    if not relevant:
        return None
    for i, doc in enumerate(retrieved[:k], start=1):
        if doc in relevant:
            return 1.0 / i
    return 0.0


def ndcg_at_k(retrieved: list, relevant: dict, k: int) -> float | None:
    """按 log2 位置折损的分级相关性得分：多相关结果且重要程度不同时的排序主指标。"""
    if not relevant:
        return None
    dcg = sum(
        relevant[doc] / math.log2(i + 2)
        for i, doc in enumerate(retrieved[:k]) if doc in relevant
    )
    ideal = sorted(relevant.values(), reverse=True)[:k]
    idcg = sum(g / math.log2(i + 2) for i, g in enumerate(ideal))
    return dcg / idcg if idcg > 0 else 0.0


def mean(values: list) -> float | None:
    """忽略 None 求均值；全空返回 None。"""
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def percentile(values: list, p: float) -> float | None:
    """线性插值百分位（p in [0, 100]）；空列表返回 None。"""
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    pos = (len(s) - 1) * p / 100
    lo, hi = math.floor(pos), math.ceil(pos)
    if lo == hi:
        return s[lo]
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)
