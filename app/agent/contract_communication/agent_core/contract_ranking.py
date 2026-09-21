"""合同候选排名融合；不混合不同检索方式的原始分值尺度。"""
from dataclasses import dataclass
import math


@dataclass(frozen=True)
class CandidateRanking:
    hits: tuple
    raw_hits: tuple
    search_type: str
    score_kind: str = 'raw'
    has_relevance: bool = True


def _ranks(hits):
    """同分共享名次，避免 ID 的展示顺序变成相关性偏差。"""
    ranks = {}; previous = None; rank = 0
    for position, (doc, score) in enumerate(hits, 1):
        if previous is None or score != previous:
            rank = position
        ranks[doc] = rank; previous = score
    return ranks


def rank_candidates(hits, *, search_type, parent=None, exact=False, k=60, history_weight=0.5):
    if type(k) is not int or k < 1 or not math.isfinite(history_weight) or not 0 <= history_weight <= 1:
        raise ValueError('RRF 参数无效')
    hits = tuple(hits)
    if not hits or len({d for d, _ in hits}) != len(hits):
        raise ValueError('候选不能为空或重复')
    if any(isinstance(s, bool) or not isinstance(s, (float, int)) or not math.isfinite(s) for _, s in hits):
        raise ValueError('候选分数必须是有限数值')
    selected = {d for d, _ in hits}
    if parent is not None and not selected <= {d for d, _ in parent.hits}:
        raise ValueError('结果越过父结果集范围')
    if exact:
        if parent is None:
            values = tuple((d, 1.0) for d in sorted(selected))
            return CandidateRanking(values, values, 'exact', 'filter', False)
        # 精确过滤继承分数、顺序和最近一次相关性原分，不制造新排序证据。
        return CandidateRanking(tuple(h for h in parent.hits if h[0] in selected),
            tuple(h for h in parent.raw_hits if h[0] in selected), parent.search_type,
            parent.score_kind, parent.has_relevance)
    raw = tuple(sorted(hits, key=lambda h: (-h[1], h[0])))
    if parent is None or not parent.has_relevance:
        return CandidateRanking(raw, raw, search_type)
    # 历史只在幸存集合中重排名；没有入选本轮的合同不参与融合。
    old = _relevance_ranks(CandidateRanking(tuple(h for h in parent.hits if h[0] in selected),
        parent.raw_hits, parent.search_type, parent.score_kind, parent.has_relevance))
    new = _ranks(raw)
    fused = tuple(sorted(((d, (history_weight / (k + old[d]) if d in old else 0.0) + (1-history_weight) / (k + new[d]))
                          for d in selected), key=lambda h: (-h[1], h[0])))
    return CandidateRanking(fused, raw, search_type, 'rrf', True)


def union_candidates(left, right, *, k=60):
    """OR 保留所有成员；每表等权，未命中或纯筛选不产生排名贡献。"""
    if type(k) is not int or k < 1:
        raise ValueError('RRF 参数无效')
    selected = {doc for doc, _ in left.hits} | {doc for doc, _ in right.hits}
    if not selected:
        raise ValueError('空结果不创建ID')
    rankings = [_relevance_ranks(entry) for entry in (left, right)]
    if not any(rankings):
        hits = tuple((doc, 1.0) for doc in sorted(selected))
        return CandidateRanking(hits, hits, 'exact', 'filter', False)
    hits = tuple(sorted(((doc, sum(0.5 / (k + ranks[doc]) for ranks in rankings if doc in ranks))
                         for doc in selected), key=lambda item: (-item[1], item[0])))
    # union 的 raw_hits 表示本次并集RRF，而非某一来源的余弦或BM25。
    return CandidateRanking(hits, hits, 'union', 'rrf', True)


def _relevance_ranks(entry):
    if not entry.has_relevance:
        return {}
    # 混合并集中零分表示仅由纯筛选召回，后续合并/筛选也不能凭展示位置制造证据。
    return _ranks(tuple((doc, score) for doc, score in entry.hits
                        if entry.search_type != 'union' or score > 0))
