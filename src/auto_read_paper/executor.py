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
        top_papers = pool[: self.config.executor.max_paper_num]

        if not top_papers and not self.config.executor.send_empty:
            logger.info("No papers in pool. No email will be sent.")
            if self.history is not None:
                self.history.save()
            return

        logger.info(f"Generating deep summaries for top {len(top_papers)} papers...")
        for p in tqdm(top_papers):
            p.generate_tldr(self.openai_client, self.config.llm)
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
