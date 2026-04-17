"""Multi-agent reranker: per-paper Reader produces structured notes,
then a single batch Reviewer ranks them and picks the top-K.

Pipeline:
    candidates --[keyword pre-filter]--> kept
              --[Reader x N parallel]--> notes
              --[Reviewer x 1 batch]--> ranked top-K
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

import tiktoken
from loguru import logger
from omegaconf import DictConfig, OmegaConf
from openai import OpenAI
from tqdm import tqdm

from ..protocol import Paper, CorpusPaper
from .base import BaseReranker, register_reranker
from .keyword_llm import _normalize_keywords, count_keyword_hits


READER_SYSTEM_PROMPT = (
    "You are a fast paper reader. Read the given title, abstract, and a preview of "
    "the main content, then produce CONCISE structured notes. "
    "Return ONLY a compact JSON object with keys "
    '"task", "method", "contributions", "results", "limitations". '
    "Each value should be a single sentence (<= 30 words). No prose outside the JSON."
)

REVIEWER_SYSTEM_PROMPT = (
    "You are a senior research reviewer. Given structured notes for several papers, "
    "rank them by overall value to a researcher with the stated keywords. "
    "Return ONLY a compact JSON object: "
    '{"rankings": [{"id": <int>, "score": <float 0-10>, "reason": "<one sentence>"}, ...]} '
    "ordered from best to worst. Include EVERY paper id you were given. "
    "Score reflects a holistic judgement on innovation, relevance to keywords, and likely impact."
)


def _truncate_to_tokens(text: str, max_tokens: int) -> str:
    if not text:
        return ""
    enc = tiktoken.encoding_for_model("gpt-4o")
    toks = enc.encode(text)
    if len(toks) <= max_tokens:
        return text
    return enc.decode(toks[:max_tokens])


def _parse_reader_json(content: str) -> dict | None:
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
    for k in ("task", "method", "contributions", "results", "limitations"):
        v = data.get(k)
        out[k] = str(v).strip() if v is not None else ""
    return out


def _parse_reviewer_json(content: str, expected_ids: set[int]) -> list[dict] | None:
    if not content:
        return None
    m = re.search(r"\{.*\}", content, flags=re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except Exception:
        return None
    rankings = data.get("rankings")
    if not isinstance(rankings, list):
        return None
    cleaned: list[dict] = []
    seen: set[int] = set()
    for item in rankings:
        if not isinstance(item, dict):
            continue
        try:
            pid = int(item.get("id"))
            score = float(item.get("score", 0.0))
        except (TypeError, ValueError):
            continue
        if pid not in expected_ids or pid in seen:
            continue
        seen.add(pid)
        cleaned.append({
            "id": pid,
            "score": max(0.0, min(10.0, score)),
            "reason": str(item.get("reason", ""))[:300],
        })
    return cleaned or None


@register_reranker("reader_reviewer")
class ReaderReviewerReranker(BaseReranker):
    """Two-agent reranker: Reader (per-paper, parallel) + Reviewer (batch)."""

    def __init__(self, config: DictConfig):
        super().__init__(config)
        rr_cfg = config.reranker.reader_reviewer
        self.threshold: float = float(rr_cfg.get("threshold", 0.0))
        self.concurrency: int = int(rr_cfg.get("concurrency", 4))
        self.reader_max_tokens: int = int(rr_cfg.get("reader_max_input_tokens", 3000))
        self.reviewer_max_papers: int = int(rr_cfg.get("reviewer_max_papers", 60))
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

    def get_similarity_score(self, s1, s2):  # pragma: no cover - not used
        raise NotImplementedError("reader_reviewer reranker does not use similarity scoring")

    def _read_one(self, paper: Paper) -> dict | None:
        body = ""
        if paper.title:
            body += f"Title: {paper.title}\n\n"
        if paper.abstract:
            body += f"Abstract: {paper.abstract}\n\n"
        if paper.full_text:
            body += f"Main content preview:\n{paper.full_text}\n"
        body = _truncate_to_tokens(body, self.reader_max_tokens)
        if not body.strip():
            return None
        try:
            resp = self.client.chat.completions.create(
                messages=[
                    {"role": "system", "content": READER_SYSTEM_PROMPT},
                    {"role": "user", "content": body},
                ],
                **self.model_kwargs,
            )
            content = resp.choices[0].message.content or ""
        except Exception as e:
            logger.warning(f"Reader failed for {paper.title}: {e}")
            return None
        notes = _parse_reader_json(content)
        if notes is None:
            logger.warning(f"Unparseable Reader output for {paper.title}: {content[:200]}")
        return notes

    def _build_reviewer_prompt(self, paper_notes: list[tuple[int, Paper, dict]]) -> str:
        lines = [
            f"User research keywords: {', '.join(self.keywords) if self.keywords else '(not provided)'}",
            f"Number of papers to rank: {len(paper_notes)}",
            "",
            "Papers:",
        ]
        for pid, paper, note in paper_notes:
            lines.append(f"--- id: {pid} ---")
            lines.append(f"Title: {paper.title}")
            lines.append(f"Task: {note.get('task', '')}")
            lines.append(f"Method: {note.get('method', '')}")
            lines.append(f"Contributions: {note.get('contributions', '')}")
            lines.append(f"Results: {note.get('results', '')}")
            lines.append(f"Limitations: {note.get('limitations', '')}")
            lines.append("")
        lines.append(
            "Return JSON only: "
            '{"rankings": [{"id": <int>, "score": <float 0-10>, "reason": "..."}, ...]} '
            "ordered best-first, including every id above."
        )
        return "\n".join(lines)

    def _review_batch(self, paper_notes: list[tuple[int, Paper, dict]]) -> list[dict] | None:
        if not paper_notes:
            return None
        expected_ids = {pid for pid, _, _ in paper_notes}
        prompt = self._build_reviewer_prompt(paper_notes)
        try:
            resp = self.client.chat.completions.create(
                messages=[
                    {"role": "system", "content": REVIEWER_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                **self.model_kwargs,
            )
            content = resp.choices[0].message.content or ""
        except Exception as e:
            logger.warning(f"Reviewer batch failed: {e}")
            return None
        rankings = _parse_reviewer_json(content, expected_ids)
        if rankings is None:
            logger.warning(f"Unparseable Reviewer output: {content[:300]}")
        return rankings

    def rerank(self, candidates: list[Paper], corpus: list[CorpusPaper]) -> list[Paper]:
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

        # Cap how many go to the Reviewer (token budget)
        if len(candidates) > self.reviewer_max_papers:
            logger.info(
                f"Trimming candidates to first {self.reviewer_max_papers} for the Reviewer "
                f"(was {len(candidates)})"
            )
            candidates = candidates[: self.reviewer_max_papers]

        logger.info(
            f"Reader agent: reading {len(candidates)} papers (concurrency={self.concurrency})..."
        )
        notes_by_idx: dict[int, dict] = {}
        with ThreadPoolExecutor(max_workers=max(1, self.concurrency)) as ex:
            futures = {ex.submit(self._read_one, p): i for i, p in enumerate(candidates)}
            for fut in tqdm(as_completed(futures), total=len(futures), desc="Reading"):
                i = futures[fut]
                try:
                    note = fut.result()
                except Exception as e:
                    logger.warning(f"Reader worker raised: {e}")
                    note = None
                if note is not None:
                    notes_by_idx[i] = note

        paper_notes = [(i, candidates[i], notes_by_idx[i]) for i in sorted(notes_by_idx)]
        logger.info(f"Reader agent: {len(paper_notes)}/{len(candidates)} papers produced notes")
        if not paper_notes:
            logger.warning("Reader produced no notes; returning unranked candidates.")
            for p in candidates:
                p.score = 0.0
            return candidates

        logger.info(f"Reviewer agent: ranking {len(paper_notes)} papers in one batch call...")
        rankings = self._review_batch(paper_notes)
        if rankings is None:
            logger.warning(
                "Reviewer failed; falling back to keyword-hit count for ordering."
            )
            for p in candidates:
                p.score = float(count_keyword_hits(p, self.keywords))
            ranked = sorted(candidates, key=lambda p: p.score or 0.0, reverse=True)
            return ranked

        score_by_id = {r["id"]: r for r in rankings}
        for i, paper, _note in paper_notes:
            entry = score_by_id.get(i)
            paper.score = entry["score"] if entry else 0.0
            if entry and entry.get("reason"):
                logger.debug(f"[{paper.score:.2f}] {paper.title[:80]} — {entry['reason']}")

        # Ordered as the Reviewer ranked
        results: list[Paper] = []
        for r in rankings:
            paper = candidates[r["id"]]
            if (paper.score or 0.0) >= self.threshold:
                results.append(paper)

        logger.info(
            f"Reranked: {len(results)} papers passed threshold {self.threshold}"
        )
        return results
