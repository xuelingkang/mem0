"""
Scoring utilities for hybrid retrieval.

Provides:
- **BM25 normalization**: Sigmoid normalization of raw BM25 scores to [0, 1].
- **BM25 parameter selection**: Query-length-adaptive sigmoid parameters.
- **Additive scoring**: Combined scoring with semantic + BM25 + entity boost.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional


def get_bm25_params(query: str, *, lemmatized: Optional[str] = None) -> tuple:
    """Get BM25 sigmoid parameters based on query length.

    Longer queries tend to have higher raw BM25 scores, so we adjust
    the sigmoid midpoint and steepness accordingly.

    Returns:
        (midpoint, steepness) for sigmoid normalization.
    """
    if lemmatized is None:
        from mem0.utils.lemmatization import lemmatize_for_bm25

        lemmatized = lemmatize_for_bm25(query)
    num_terms = len(lemmatized.split()) if lemmatized else 1

    if num_terms <= 3:
        return 5.0, 0.7
    elif num_terms <= 6:
        return 7.0, 0.6
    elif num_terms <= 9:
        return 9.0, 0.5
    elif num_terms <= 15:
        return 10.0, 0.5
    else:
        return 12.0, 0.5


def normalize_bm25(raw_score: float, midpoint: float, steepness: float) -> float:
    """Normalize BM25 score to [0, 1] using logistic sigmoid.

    Args:
        raw_score: Raw BM25 score (unbounded, typically 0-20+).
        midpoint: Score at which sigmoid outputs 0.5.
        steepness: Controls how quickly sigmoid transitions.

    Returns:
        Normalized score in range [0, 1].
    """
    return 1.0 / (1.0 + math.exp(-steepness * (raw_score - midpoint)))


ENTITY_BOOST_WEIGHT = 0.5

# 图加分上限 W_g。与实体加入上限同量级：四信号全开时图信号对最终分的贡献上限为
# `0.5 / 3.0 = 16.7%`（设计 §6.2）。
GRAPH_BOOST_WEIGHT = 0.5

# Time-factor components published in `score_details` when decay is active. The scorer
# only copies them through, so the field names stay owned by the decay implementation.
DECAY_DETAIL_KEYS = (
    "decay_weight",
    "retention",
    "memory_strength_days",
    "elapsed_days",
    "access_count",
)

# Graph-signal components published in `score_details` when the graph capability is on.
# `graph_boost` is the candidate's additive boost (0.0 when the graph missed it);
# `graph_facts` is how many graph facts referenced it. Both keys stay absent when the
# capability is off, so `explain` output is byte-identical to pre-graph releases.
GRAPH_DETAIL_KEYS = ("graph_boost", "graph_facts")


def score_and_rank(
    semantic_results: List[Dict[str, Any]],
    bm25_scores: Dict[str, float],
    entity_boosts: Dict[str, float],
    threshold: float,
    top_k: int,
    explain: bool = False,
    decay_factors: Optional[Dict[str, Dict[str, Any]]] = None,
    graph_boosts: Optional[Dict[str, float]] = None,
    graph_facts: Optional[Dict[str, int]] = None,
) -> List[Dict[str, Any]]:
    """Score candidates additively and return top-k results.

    For each candidate:
        semantic_score is taken from the result's score field.
        combined = (semantic + bm25 + entity_boost + graph_boost) / max_possible
        final_score = combined * decay_weight

    Threshold gates the semantic score BEFORE combining -- candidates
    below the threshold are excluded even if BM25/entity would boost them.

    The divisor adapts based on which signals are active:
        - Semantic only: max_possible = 1.0
        - Semantic + BM25: max_possible = 2.0
        - Semantic + BM25 + entity: max_possible = 2.5
        - Semantic + entity (no BM25): max_possible = 1.5
        - ... plus `GRAPH_BOOST_WEIGHT` whenever at least one candidate in the pool
          actually carries a graph boost.

    The time factor is applied after combining and before ranking, so it can only
    reorder candidates that are already in the pool: it is a multiplier bounded by
    [floor, 1].0 in the caller, never a filter.

    Args:
        semantic_results: Candidate memories from vector search.
        bm25_scores: Normalized keyword scores keyed by memory ID.
        entity_boosts: Entity-link boosts keyed by memory ID.
        threshold: Minimum semantic score required before hybrid scoring.
        top_k: Maximum number of results to return.
        explain: Include score_details in each result when true.
        decay_factors: Optional per-candidate time factor keyed by memory ID, each entry
            carrying `decay_weight` plus the explanatory components listed in
            `DECAY_DETAIL_KEYS`. Candidates absent from the mapping (and every candidate
            when the mapping is omitted or empty) score exactly as they did before the
            time factor existed.
        graph_boosts: Optional per-candidate graph boost keyed by memory ID. The caller
            passes the mapping only when the graph capability is ON: an empty mapping
            then means "graph is on, nothing was hit", which keeps the divisor at its
            base value while still publishing zero-valued graph components. `None` means
            the capability is off and the scoring is byte-identical to its pre-graph
            form. Only candidates present here grow the divisor, so ids outside the
            candidate pool can never influence it.
        graph_facts: Optional per-candidate count of graph facts that referenced it,
            published as `graph_facts` in `score_details`.

    Returns:
        List of scored result dicts sorted by combined score descending.
    """

    has_bm25 = bool(bm25_scores)
    has_entity = bool(entity_boosts)
    has_graph = bool(graph_boosts)

    max_possible = 1.0
    if has_bm25:
        max_possible += 1.0
    if has_entity:
        max_possible += ENTITY_BOOST_WEIGHT
    if has_graph:
        max_possible += GRAPH_BOOST_WEIGHT

    graph_enabled = graph_boosts is not None

    scored: List[Dict[str, Any]] = []

    for result in semantic_results:
        mem_id = result.get("id")
        if mem_id is None:
            continue

        semantic_score = result.get("score") or 0.0
        if semantic_score < threshold:
            continue

        mem_id_str = str(mem_id)
        bm25_score = bm25_scores.get(mem_id_str, 0.0)
        entity_boost = entity_boosts.get(mem_id_str, 0.0)
        graph_boost = (graph_boosts or {}).get(mem_id_str, 0.0)

        raw_combined = semantic_score + bm25_score + entity_boost + graph_boost
        combined = min(raw_combined / max_possible, 1.0)

        # Time factor: a bounded multiplier on the hybrid score, never a filter.
        decay = decay_factors.get(mem_id_str) if decay_factors else None
        final_score = combined if decay is None else combined * float(decay.get("decay_weight", 1.0))

        scored_result = {
            "id": mem_id_str,
            "score": final_score,
            "payload": result.get("payload"),
        }
        if explain:
            score_details = {
                "semantic_score": semantic_score,
                "bm25_score": bm25_score,
                "entity_boost": entity_boost,
                "raw_score": raw_combined,
                "max_possible_score": max_possible,
                "final_score": final_score,
                "threshold": threshold,
            }
            if decay is not None:
                # 五项分量：时间因子本身 + 四项可解释输入（设计 §5.3）。关闭态下不出现，
                # 使 explain 输出与引入本机制之前逐位一致。
                for key in DECAY_DETAIL_KEYS:
                    score_details[key] = decay.get(key)
            if graph_enabled:
                # 图能力开启即发布两个分量键：未命中为 0.0 / 0，命中为实际加分与事实条数
                # （设计 §8）。关闭态不发布，`explain` 与引入本机制之前逐位一致。
                score_details[GRAPH_DETAIL_KEYS[0]] = graph_boost
                score_details[GRAPH_DETAIL_KEYS[1]] = (graph_facts or {}).get(mem_id_str, 0)
            scored_result["score_details"] = score_details
        scored.append(scored_result)

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_k]
