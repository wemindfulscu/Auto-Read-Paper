from .base import BaseRetriever, register_retriever
import arxiv
from arxiv import Result as ArxivResult
from ..protocol import Paper
from ..utils import extract_markdown_from_pdf, extract_tex_code_from_tar
from tempfile import TemporaryDirectory
import feedparser
from tqdm import tqdm
import multiprocessing
import os
import logging
from queue import Empty
from typing import Any, Callable, TypeVar
from loguru import logger
import requests

# arXiv's HTML rendering service (arxiv.org/html/<id>) is often unavailable for
# freshly-announced papers. trafilatura logs a noisy ERROR line per 404 that
# isn't actionable — we already fall back to tar/PDF extraction. Mute it.
logging.getLogger("trafilatura").setLevel(logging.CRITICAL)
logging.getLogger("trafilatura.downloads").setLevel(logging.CRITICAL)

T = TypeVar("T")

DOWNLOAD_TIMEOUT = (10, 60)
PDF_EXTRACT_TIMEOUT = 180
TAR_EXTRACT_TIMEOUT = 180


def _download_file(url: str, path: str) -> None:
    with requests.get(url, stream=True, timeout=DOWNLOAD_TIMEOUT) as response:
        response.raise_for_status()
        with open(path, "wb") as file:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    file.write(chunk)


def _run_in_subprocess(
    result_queue: Any,
    func: Callable[..., T | None],
    args: tuple[Any, ...],
) -> None:
    try:
        result_queue.put(("ok", func(*args)))
    except Exception as exc:
        result_queue.put(("error", f"{type(exc).__name__}: {exc}"))


def _run_with_hard_timeout(
    func: Callable[..., T | None],
    args: tuple[Any, ...],
    *,
    timeout: float,
    operation: str,
    paper_title: str,
) -> T | None:
    start_methods = multiprocessing.get_all_start_methods()
    context = multiprocessing.get_context("fork" if "fork" in start_methods else start_methods[0])
    result_queue = context.Queue()
    process = context.Process(target=_run_in_subprocess, args=(result_queue, func, args))
    process.start()

    try:
        status, payload = result_queue.get(timeout=timeout)
    except Empty:
        if process.is_alive():
            process.kill()
        process.join(5)
        result_queue.close()
        result_queue.join_thread()
        logger.warning(f"{operation} timed out for {paper_title} after {timeout} seconds")
        return None

    process.join(5)
    result_queue.close()
    result_queue.join_thread()

    if status == "ok":
        return payload

    logger.warning(f"{operation} failed for {paper_title}: {payload}")
    return None


def _extract_text_from_pdf_worker(pdf_url: str) -> str:
    with TemporaryDirectory() as temp_dir:
        path = os.path.join(temp_dir, "paper.pdf")
        _download_file(pdf_url, path)
        return extract_markdown_from_pdf(path)


def _extract_text_from_html_worker(html_url: str) -> str | None:
    import trafilatura

    downloaded = trafilatura.fetch_url(html_url)
    if downloaded is None:
        raise ValueError(f"Failed to download HTML from {html_url}")
    text = trafilatura.extract(downloaded, include_comments=False, include_tables=False)
    if not text:
        raise ValueError(f"No text extracted from {html_url}")
    return text


def _extract_text_from_tar_worker(source_url: str, paper_id: str, paper_title: str | None = None) -> str | None:
    with TemporaryDirectory() as temp_dir:
        path = os.path.join(temp_dir, "paper.tar.gz")
        _download_file(source_url, path)
        file_contents = extract_tex_code_from_tar(path, paper_id, paper_title=paper_title)
        if not file_contents or "all" not in file_contents:
            raise ValueError("Main tex file not found.")
        return file_contents["all"]


@register_retriever("arxiv")
class ArxivRetriever(BaseRetriever):
    def __init__(self, config):
        super().__init__(config)
        if self.config.source.arxiv.category is None:
            raise ValueError("category must be specified for arxiv.")

    def retrieve_recent_fallback(self, days: int = 3, limit: int = 10) -> list[ArxivResult]:
        """Last-resort fetch: query arXiv API for recent papers in configured categories.

        Used only when the primary RSS + history pool is empty (e.g. first-run empty
        history + quiet day). Samples papers submitted in the last ``days`` days,
        applies the keyword filter, and returns up to ``limit`` of them.
        """
        import random

        client = arxiv.Client(num_retries=5, delay_seconds=5)
        categories = list(self.config.source.arxiv.category)
        cat_query = " OR ".join(f"cat:{c}" for c in categories)
        # Pull generously, then filter by keywords + randomly sample.
        search = arxiv.Search(
            query=cat_query,
            max_results=200,
            sort_by=arxiv.SortCriterion.SubmittedDate,
            sort_order=arxiv.SortOrder.Descending,
        )
        try:
            results = list(client.results(search))
        except Exception as exc:
            logger.warning(f"Fallback arXiv query failed: {exc}")
            return []

        keywords_cfg = self.config.source.arxiv.get("keywords")
        keywords = (
            [str(k).strip().lower() for k in keywords_cfg if str(k).strip()]
            if keywords_cfg else []
        )
        if keywords:
            results = [
                r for r in results
                if any(kw in f"{r.title or ''} {r.summary or ''}".lower() for kw in keywords)
            ]

        # Keep only papers submitted within the window; arXiv results are already
        # sorted desc by date, so the tail gets trimmed implicitly via the limit.
        if results:
            newest = results[0].published
            from datetime import timedelta
            cutoff = newest - timedelta(days=days)
            results = [r for r in results if r.published >= cutoff]

        if not results:
            return []

        sample_size = min(limit, len(results))
        sampled = random.sample(results, sample_size)
        logger.info(
            f"Fallback retrieval: {len(sampled)} paper(s) sampled from last {days}d "
            f"(keyword-matched pool size {len(results)})"
        )
        return sampled

    def _retrieve_raw_papers(self) -> list[ArxivResult]:
        client = arxiv.Client(num_retries=10, delay_seconds=10)
        query = '+'.join(self.config.source.arxiv.category)
        include_cross_list = self.config.source.arxiv.get("include_cross_list", False)
        # Get the latest paper from arxiv rss feed
        feed = feedparser.parse(f"https://rss.arxiv.org/atom/{query}")
        if 'Feed error for query' in feed.feed.title:
            raise Exception(f"Invalid ARXIV_QUERY: {query}.")
        raw_papers = []
        allowed_announce_types = {"new", "cross"} if include_cross_list else {"new"}
        all_paper_ids = [
            i.id.removeprefix("oai:arXiv.org:")
            for i in feed.entries
            if i.get("arxiv_announce_type", "new") in allowed_announce_types
        ]
        if self.config.executor.debug:
            all_paper_ids = all_paper_ids[:10]

        # Get full information of each paper from arxiv api
        bar = tqdm(total=len(all_paper_ids))
        for i in range(0, len(all_paper_ids), 20):
            search = arxiv.Search(id_list=all_paper_ids[i:i + 20])
            batch = list(client.results(search))
            bar.update(len(batch))
            raw_papers.extend(batch)
        bar.close()

        # Optional keyword pre-filter on title + abstract (case-insensitive substring)
        keywords_cfg = self.config.source.arxiv.get("keywords")
        if keywords_cfg:
            keywords = [str(k).strip().lower() for k in keywords_cfg if str(k).strip()]
            if keywords:
                before = len(raw_papers)
                raw_papers = [
                    r for r in raw_papers
                    if any(
                        kw in f"{r.title or ''} {r.summary or ''}".lower()
                        for kw in keywords
                    )
                ]
                logger.info(
                    f"arXiv keyword pre-filter: {len(raw_papers)}/{before} papers match {keywords}"
                )

        return raw_papers

    def retrieve_fallback_papers(self, days: int = 3, limit: int = 5) -> list[Paper]:
        """Convenience wrapper: fallback raw results → Paper objects."""
        raws = self.retrieve_recent_fallback(days=days, limit=limit)
        papers: list[Paper] = []
        for r in raws:
            try:
                papers.append(self.convert_to_paper(r))
            except Exception as exc:
                logger.warning(f"Skipping fallback paper {getattr(r, 'title', r)}: {exc}")
        return papers

    def convert_to_paper(self, raw_paper: ArxivResult) -> Paper:
        title = raw_paper.title
        authors = [a.name for a in raw_paper.authors]
        abstract = raw_paper.summary
        pdf_url = raw_paper.pdf_url
        full_text = extract_text_from_tar(raw_paper)
        if full_text is None:
            full_text = extract_text_from_html(raw_paper)
        if full_text is None:
            full_text = extract_text_from_pdf(raw_paper)
        return Paper(
            source=self.name,
            title=title,
            authors=authors,
            abstract=abstract,
            url=raw_paper.entry_id,
            pdf_url=pdf_url,
            full_text=full_text,
        )


def extract_text_from_html(paper: ArxivResult) -> str | None:
    html_url = paper.entry_id.replace("/abs/", "/html/")
    try:
        return _extract_text_from_html_worker(html_url)
    except Exception as exc:
        # arXiv's HTML service is routinely unavailable for brand-new papers.
        # Demote to DEBUG — the tar/PDF fallback will handle extraction and
        # users don't need to see a WARNING for expected behavior.
        logger.debug(f"HTML extraction unavailable for {paper.title}: {exc}")
        return None


def extract_text_from_pdf(paper: ArxivResult) -> str | None:
    if paper.pdf_url is None:
        logger.warning(f"No PDF URL available for {paper.title}")
        return None
    return _run_with_hard_timeout(
        _extract_text_from_pdf_worker,
        (paper.pdf_url,),
        timeout=PDF_EXTRACT_TIMEOUT,
        operation="PDF extraction",
        paper_title=paper.title,
    )


def extract_text_from_tar(paper: ArxivResult) -> str | None:
    source_url = paper.source_url()
    if source_url is None:
        logger.warning(f"No source URL available for {paper.title}")
        return None
    return _run_with_hard_timeout(
        _extract_text_from_tar_worker,
        (source_url, paper.entry_id, paper.title),
        timeout=TAR_EXTRACT_TIMEOUT,
        operation="Tar extraction",
        paper_title=paper.title,
    )
