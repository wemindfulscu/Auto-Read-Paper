from loguru import logger
from omegaconf import DictConfig
from .retriever import get_retriever_cls
from .reranker import get_reranker_cls
from .construct_email import render_email
from .utils import send_email
from .history import ScoreHistory, _today_iso
from openai import OpenAI
from tqdm import tqdm


class Executor:
    def __init__(self, config: DictConfig):
        self.config = config
        self.retrievers = {
            source: get_retriever_cls(source)(config) for source in config.executor.source
        }
        self.reranker = get_reranker_cls(config.executor.reranker)(config)
        self.openai_client = OpenAI(api_key=config.llm.api.key, base_url=config.llm.api.base_url)

        hist_cfg = config.get("history") if hasattr(config, "get") else None
        self.history: ScoreHistory | None = None
        if hist_cfg is not None and bool(hist_cfg.get("enabled", True)):
            self.history = ScoreHistory(
                path=str(hist_cfg.get("path", "state/score_history.json")),
                retention_days=int(hist_cfg.get("retention_days", 7)),
            )

    def run(self):
        today = _today_iso()

        if self.history is not None:
            self.history.load()
            self.history.trim()

        all_papers = []
        for source, retriever in self.retrievers.items():
            logger.info(f"Retrieving {source} papers...")
            papers = retriever.retrieve_papers()
            if len(papers) == 0:
                logger.info(f"No {source} papers found")
                continue
            logger.info(f"Retrieved {len(papers)} {source} papers")
            all_papers.extend(papers)
        logger.info(f"Total {len(all_papers)} papers retrieved from all sources")

        # Skip papers we've already scored within the retention window.
        if self.history is not None:
            all_papers = self.history.filter_new_papers(all_papers)
            logger.info(f"{len(all_papers)} new papers need scoring today")

        # Score today's new papers.
        scored_today: list = []
        if all_papers:
            logger.info("Reranking papers (keyword filter + LLM scoring)...")
            scored_today = self.reranker.rerank(all_papers, [])

        # Record today's scores, then merge with unsent history into the candidate pool.
        if self.history is not None:
            self.history.record_newly_scored(scored_today, today)
            pool = self.history.unsent_papers()
            logger.info(
                f"Candidate pool for today's email: {len(pool)} papers "
                f"(today={len(scored_today)} + unsent history)"
            )
        else:
            pool = list(scored_today)

        pool.sort(key=lambda p: p.score or 0.0, reverse=True)
        max_n = int(self.config.executor.max_paper_num)
        top_papers = pool[:max_n]

        # Fallback so the daily email is never empty: if we don't have enough
        # unsent/fresh papers, pad with previously-sent entries from history
        # (highest-scoring first). This guarantees the pipeline produces a
        # visible heartbeat every day even on quiet days.
        if len(top_papers) < max_n and self.history is not None:
            already_ids = {getattr(p, "url", None) for p in top_papers}
            filler_pool = [
                p for p in self.history.sent_papers()
                if getattr(p, "url", None) not in already_ids
            ]
            filler_pool.sort(key=lambda p: p.score or 0.0, reverse=True)
            needed = max_n - len(top_papers)
            filler = filler_pool[:needed]
            if filler:
                logger.info(
                    f"Padding email with {len(filler)} previously-sent paper(s) "
                    f"as fallback (primary pool had only {len(top_papers)})"
                )
                top_papers.extend(filler)

        # Last-resort fallback: still nothing (e.g. first run with empty history on a
        # quiet day). Pull a few recent arXiv papers so the pipeline proves it's alive.
        if not top_papers:
            arxiv_retriever = self.retrievers.get("arxiv")
            if arxiv_retriever is not None and hasattr(arxiv_retriever, "retrieve_fallback_papers"):
                logger.info("Pool empty — fetching recent arXiv papers as heartbeat fallback")
                try:
                    fb = arxiv_retriever.retrieve_fallback_papers(days=3, limit=max_n)
                except Exception as exc:
                    logger.warning(f"Heartbeat fallback failed: {exc}")
                    fb = []
                if fb:
                    # Score them so the email still shows a ranked number.
                    logger.info(f"Scoring {len(fb)} heartbeat papers")
                    fb = self.reranker.rerank(fb, [])
                    fb.sort(key=lambda p: p.score or 0.0, reverse=True)
                    top_papers = fb[:max_n]
                    if self.history is not None:
                        self.history.record_newly_scored(top_papers, today)

        if not top_papers and not self.config.executor.send_empty:
            logger.info("No papers in pool even after fallback. No email will be sent.")
            if self.history is not None:
                self.history.save()
            return

        if top_papers:
            logger.info(f"Generating deep summaries for top {len(top_papers)} papers...")
            for p in tqdm(top_papers):
                # Skip re-generating tldr for previously-rendered fillers that
                # already have it from a past run — saves tokens.
                if not p.tldr:
                    p.generate_tldr(self.openai_client, self.config.llm)
                if not p.affiliations:
                    p.generate_affiliations(self.openai_client, self.config.llm)

        logger.info("Sending email...")
        email_content = render_email(top_papers)
        send_email(self.config, email_content)
        logger.info("Email sent successfully")

        # Only mark as sent AFTER SMTP succeeds — if the send fails, the papers
        # stay in the unsent pool and get another shot tomorrow.
        if self.history is not None:
            self.history.mark_sent(top_papers, today)
            self.history.save()
