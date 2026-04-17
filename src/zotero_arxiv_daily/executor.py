from loguru import logger
from omegaconf import DictConfig
from .retriever import get_retriever_cls
from .reranker import get_reranker_cls
from .construct_email import render_email
from .utils import send_email
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

    def run(self):
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

        reranked_papers = []
        if len(all_papers) > 0:
            logger.info("Reranking papers (keyword filter + LLM scoring)...")
            reranked_papers = self.reranker.rerank(all_papers, [])
            reranked_papers = reranked_papers[: self.config.executor.max_paper_num]
            logger.info(f"Generating deep summaries for top {len(reranked_papers)} papers...")
            for p in tqdm(reranked_papers):
                p.generate_tldr(self.openai_client, self.config.llm)
                p.generate_affiliations(self.openai_client, self.config.llm)
        elif not self.config.executor.send_empty:
            logger.info("No new papers found. No email will be sent.")
            return

        logger.info("Sending email...")
        email_content = render_email(reranked_papers)
        send_email(self.config, email_content)
        logger.info("Email sent successfully")
