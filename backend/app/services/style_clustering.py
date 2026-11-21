"""
Style Clustering Service

Clusters style feature vectors from a reference book using DBSCAN/KMeans,
then generates StyleProfile configs from cluster centroids.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass

import numpy as np
from sklearn.cluster import DBSCAN, KMeans
from sklearn.preprocessing import StandardScaler

logger = logging.getLogger(__name__)


@dataclass
class StyleProfileConfig:
    """Generated style profile configuration."""
    name: str
    vocab_whitelist: list[str]  # preferred words
    vocab_blacklist: list[str]  # words to avoid
    sentence_ratio: dict[str, float]  # short/medium/long proportions
    dialogue_ratio: float
    rhetoric_profile: dict[str, float]
    paragraph_rhythm_pattern: str
    pov_type: str
    sample_block_ids: list[str]


def cluster_style_features(
    features: list[dict],
    block_ids: list[str],
    method: str = "dbscan",
    n_clusters: int = 3,
) -> list[StyleProfileConfig]:
    """
    Cluster style features and generate StyleProfile configs.

    Args:
        features: List of style feature dicts (from StyleExtractor)
        block_ids: Corresponding block IDs
        method: "dbscan" or "kmeans"
        n_clusters: Number of clusters for KMeans

    Returns:
        List of StyleProfileConfig objects
    """
    if len(features) < 5:
        logger.warning("Too few samples (%d) for clustering", len(features))
        return [_build_profile_from_all(features, block_ids, "Default Style")]

    # Build feature matrix
    matrix = _build_feature_matrix(features)
    scaler = StandardScaler()
    scaled = scaler.fit_transform(matrix)

    # Cluster
    if method == "dbscan":
        clusterer = DBSCAN(eps=0.8, min_samples=3)
        labels = clusterer.fit_predict(scaled)
    else:
        n_clusters = min(n_clusters, len(features))
        clusterer = KMeans(n_clusters=n_clusters, random_state=42, n_init=10)
        labels = clusterer.fit_predict(scaled)

    # Group by cluster
    clusters: dict[int, list[int]] = {}
    for idx, label in enumerate(labels):
        if label == -1:
            continue  # DBSCAN noise
        clusters.setdefault(label, []).append(idx)

    # Generate profile for each cluster
    profiles: list[StyleProfileConfig] = []
    for cluster_id, indices in sorted(clusters.items()):
        cluster_features = [features[i] for i in indices]
        cluster_block_ids = [block_ids[i] for i in indices]
        profile = _build_profile_from_cluster(
            cluster_features,
            cluster_block_ids,
            f"Style Cluster {cluster_id + 1}",
        )
        profiles.append(profile)

    if not profiles:
        profiles = [_build_profile_from_all(features, block_ids, "Default Style")]

    return profiles


def _build_feature_matrix(features: list[dict]) -> np.ndarray:
    """Convert style feature dicts to a numeric matrix."""
    rows = []
    for f in features:
        row = [
            f.get("avg_sentence_length", 0),
            f.get("sentence_length_variance", 0),
            f.get("dialogue_ratio", 0),
            f.get("narration_ratio", 0),
            f.get("description_ratio", 0),
            f.get("rhetoric_frequency", {}).get("simile", 0),
            f.get("rhetoric_frequency", {}).get("parallelism", 0),
            f.get("rhetoric_frequency", {}).get("rhetorical_question", 0),
        ]
        rows.append(row)
    return np.array(rows, dtype=float)


def _build_profile_from_cluster(
    features: list[dict],
    block_ids: list[str],
    name: str,
) -> StyleProfileConfig:
    """Build a StyleProfile from a cluster of similar text blocks."""
    # Aggregate top words
    word_counter: Counter[str] = Counter()
    for f in features:
        for word, count in f.get("top_words", []):
            word_counter[word] += count

    top_50 = [w for w, _ in word_counter.most_common(50)]

    # Average numeric features
    n = len(features)
    avg_sent_len = sum(f.get("avg_sentence_length", 0) for f in features) / n
    avg_dialogue = sum(f.get("dialogue_ratio", 0) for f in features) / n

    # Sentence length ratio
    short = sum(1 for f in features if f.get("avg_sentence_length", 0) < 15) / n
    medium = sum(1 for f in features if 15 <= f.get("avg_sentence_length", 0) < 30) / n
    long_r = 1 - short - medium

    # Rhetoric averages
    rhetoric = {}
    for key in ["simile", "parallelism", "rhetorical_question"]:
        rhetoric[key] = sum(
            f.get("rhetoric_frequency", {}).get(key, 0) for f in features
        ) / n

    # Most common POV
    pov_counter = Counter(f.get("pov_type", "third_person") for f in features)
    pov = pov_counter.most_common(1)[0][0]

    # Most common rhythm pattern
    rhythm_counter = Counter(f.get("paragraph_rhythm", "") for f in features)
    rhythm = rhythm_counter.most_common(1)[0][0]

    # Sample blocks (up to 5)
    samples = block_ids[:5]

    return StyleProfileConfig(
        name=name,
        vocab_whitelist=top_50[:30],
        vocab_blacklist=[],
        sentence_ratio={"short": round(short, 2), "medium": round(medium, 2), "long": round(long_r, 2)},
        dialogue_ratio=round(avg_dialogue, 2),
        rhetoric_profile=rhetoric,
        paragraph_rhythm_pattern=rhythm,
        pov_type=pov,
        sample_block_ids=samples,
    )


def _build_profile_from_all(
    features: list[dict],
    block_ids: list[str],
    name: str,
) -> StyleProfileConfig:
    """Build a single profile from all features (fallback when clustering fails)."""
    return _build_profile_from_cluster(features, block_ids, name)
