from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
from loguru import logger
from omegaconf import DictConfig, OmegaConf
from openai import OpenAI
from tqdm import tqdm

from ..protocol import Paper, CorpusPaper
from .base import BaseReranker, register_reranker


SCORE_SYSTEM_PROMPT = (
    "You are a senior AI research reviewer. You rate a paper on three dimensions "
    "(innovation, relevance, potential), each on a 0-10 integer scale. "
    "Return ONLY a compact JSON object with keys "
    '"innovation", "relevance", "potential", "reason". '
    'Example: {"innovation": 8, "relevance": 7, "potential": 6, "reason": "..."}'
)


def _normalize_keywords(raw) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        items = [raw]
    else:
        items = list(raw)
    return [k.strip().lower() for k in items if isinstance(k, str) and k.strip()]


def count_keyword_hits(paper: Paper, keywords: list[str]) -> int:
    if not keywords:
        return 0
    text = f"{paper.title or ''} {paper.abstract or ''}".lower()
    return sum(1 for kw in keywords if kw in text)


def _parse_score_json(content: str) -> dict[str, float] | None:
    if not content:
        return None
    m = re.search(r"\{.*\}", content, flags=re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except Exception:
        return None
    out = {}
    for k in ("innovation", "relevance", "potential"):
        v = data.get(k)
        if isinstance(v, (int, float)):
            out[k] = float(max(0, min(10, v)))
        else:
            return None
    out["reason"] = str(data.get("reason", ""))[:300]
    return out


@register_reranker("keyword_llm")
class KeywordLLMReranker(BaseReranker):
    """
    Rerank papers using LLM-rated scores on three dimensions.
    Does NOT require Zotero corpus. Designed for users who filter by keywords.
    """

    def __init__(self, config: DictConfig):
        super().__init__(config)
        rr_cfg = config.reranker.keyword_llm
        self.threshold: float = float(rr_cfg.get("threshold", 0.0))
        weights = OmegaConf.to_container(rr_cfg.weights, resolve=True) or {}
        self.weights: dict[str, float] = {
            "innovation": float(weights.get("innovation", 0.4)),
            "relevance": float(weights.get("relevance", 0.4)),
            "potential": float(weights.get("potential", 0.2)),
        }
        self.concurrency: int = int(rr_cfg.get("concurrency", 4))
        self.keyword_boost: float = float(rr_cfg.get("keyword_boost", 0.0))
        self.keywords = _normalize_keywords(
            OmegaConf.to_container(config.source.arxiv.get("keywords"), resolve=True)
            if config.source.arxiv.get("keywords") is not None
            else None
        )
        self.client = OpenAI(
            api_key=config.llm.api.key,
            base_url=config.llm.api.base_url,
        )
        self.model_kwargs = OmegaConf.to_container(
            config.llm.generation_kwargs, resolve=True
        ) or {}
        self.language = config.llm.get("language", "Chinese")

    def get_similarity_score(self, s1, s2):  # pragma: no cover - not used
        raise NotImplementedError("keyword_llm reranker does not use similarity scoring")

    def _score_one(self, paper: Paper) -> dict[str, float] | None:
        user_prompt = (
            f"Rate the following paper. User research keywords: "
            f"{', '.join(self.keywords) if self.keywords else '(not provided)'}.\n\n"
            f"Title: {paper.title}\n\nAbstract: {paper.abstract}\n\n"
            f"Scoring rubric:\n"
            f"- innovation (0-10): novelty of method/idea\n"
            f"- relevance (0-10): alignment with the user's keywords\n"
            f"- potential (0-10): likely real-world or research impact\n"
            f"Return JSON only."
        )
        try:
            resp = self.client.chat.completions.create(
                messages=[
                    {"role": "system", "content": SCORE_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                **self.model_kwargs,
            )
            content = resp.choices[0].message.content or ""
        except Exception as e:
            logger.warning(f"LLM scoring failed for {paper.title}: {e}")
            return None
        parsed = _parse_score_json(content)
        if parsed is None:
            logger.warning(f"Unparseable LLM score for {paper.title}: {content[:200]}")
        return parsed

    def rerank(self, candidates: list[Paper], corpus: list[CorpusPaper]) -> list[Paper]:
        # corpus is ignored — this reranker does not use Zotero
        if not candidates:
            return []

        # Belt & suspenders: keyword pre-filter (retriever may already have done this)
        if self.keywords:
            filtered = [p for p in candidates if count_keyword_hits(p, self.keywords) > 0]
            logger.info(
                f"Keyword pre-filter: {len(filtered)}/{len(candidates)} papers kept "
                f"(keywords={self.keywords})"
            )
            candidates = filtered

        if not candidates:
            return []

        logger.info(f"LLM scoring {len(candidates)} papers (concurrency={self.concurrency})...")
        scored: list[tuple[Paper, dict[str, float] | None]] = [(p, None) for p in candidates]
        with ThreadPoolExecutor(max_workers=max(1, self.concurrency)) as ex:
            futures = {ex.submit(self._score_one, p): i for i, p in enumerate(candidates)}
            for fut in tqdm(as_completed(futures), total=len(futures), desc="Scoring"):
                i = futures[fut]
                try:
                    scored[i] = (candidates[i], fut.result())
                except Exception as e:
                    logger.warning(f"Scoring worker raised: {e}")
                    scored[i] = (candidates[i], None)

        results: list[Paper] = []
        for paper, s in scored:
            if s is None:
                paper.score = 0.0
                continue
            composite = (
                s["innovation"] * self.weights["innovation"]
                + s["relevance"] * self.weights["relevance"]
                + s["potential"] * self.weights["potential"]
            )
            if self.keyword_boost > 0 and self.keywords:
                hits = count_keyword_hits(paper, self.keywords)
                composite += self.keyword_boost * max(0, hits - 1)
            paper.score = float(np.clip(composite, 0.0, 10.0))
            if s.get("reason"):
                logger.debug(f"[{paper.score:.2f}] {paper.title[:80]} — {s['reason']}")
            results.append(paper)

        results = [p for p in results if (p.score or 0.0) >= self.threshold]
        results.sort(key=lambda x: x.score or 0.0, reverse=True)
        logger.info(
            f"Reranked: {len(results)} papers passed threshold {self.threshold}"
        )
        return results
