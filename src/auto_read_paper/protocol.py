from dataclasses import dataclass
from typing import Optional, TypeVar
from datetime import datetime
import re
import tiktoken
from openai import OpenAI
from loguru import logger
import json
RawPaperItem = TypeVar('RawPaperItem')

_SECTION_LABELS = ("【核心工作】", "【主要创新】", "【潜在价值】")


def _clean_tldr(raw: str) -> str:
    """Extract only the three-section TLDR from the LLM output.

    Reasoning-style models often leak chain-of-thought ("Let me write...", "Now I
    need to format...") or emit a draft plus a final restatement. We find the LAST
    occurrence of 【核心工作】 — that's the clean final answer — then slice from
    there to the end of the 【潜在价值】 section. Any preamble, meta-commentary,
    or duplicate earlier draft is discarded.
    """
    if not raw:
        return ""
    text = raw.strip().replace("\r\n", "\n")

    # Strip <think>...</think> style reasoning blocks if any model emits them.
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)

    # Find the LAST 【核心工作】 — the final clean answer, not the draft/preamble.
    core_idx = text.rfind(_SECTION_LABELS[0])
    if core_idx == -1:
        # No structured output at all — return the raw text as a fallback,
        # but still strip any obvious meta-commentary markers.
        text = re.sub(r"^(好的|好|当然|Sure|Okay|OK|Let me .*?|Now .*?|I need to .*?)[:：\n]",
                      "", text, flags=re.IGNORECASE | re.MULTILINE)
        return text.strip().replace("\n", "<br>")

    text = text[core_idx:]

    # Trim any trailing noise after the 潜在价值 section. Cut at common "end of
    # answer" markers the model might append.
    for marker in ("\n\n---", "\n---", "\n\n##", "\n总结", "\nSummary"):
        cut = text.find(marker)
        if cut != -1:
            text = text[:cut]

    return text.strip().replace("\n", "<br>")


@dataclass
class Paper:
    source: str
    title: str
    authors: list[str]
    abstract: str
    url: str
    pdf_url: Optional[str] = None
    full_text: Optional[str] = None
    tldr: Optional[str] = None
    affiliations: Optional[list[str]] = None
    score: Optional[float] = None
    title_zh: Optional[str] = None

    def _generate_title_translation_with_llm(self, openai_client: OpenAI, llm_params: dict) -> Optional[str]:
        if not self.title:
            return None
        lang = str(llm_params.get('language', 'Chinese')).strip()
        if lang.lower() == 'english':
            return None
        response = openai_client.chat.completions.create(
            messages=[
                {
                    "role": "system",
                    "content": (
                        f"You translate academic paper titles into {lang}. "
                        f"Produce a natural, professional, concise {lang} title. "
                        f"Keep widely-used English technical abbreviations (e.g. RL, MPC, LLM, RAG, BEV, GRPO) untranslated. "
                        f"Output ONLY the translated title on a single line — no quotes, no explanation, no extra content."
                    ),
                },
                {"role": "user", "content": f"Translate: {self.title}"},
            ],
            **llm_params.get('generation_kwargs', {}),
        )
        out = (response.choices[0].message.content or "").strip()
        out = re.sub(r"<think>.*?</think>", "", out, flags=re.DOTALL | re.IGNORECASE).strip()
        out = out.strip("\"'「」“”").splitlines()[-1].strip() if out else ""
        return out or None

    def generate_title_zh(self, openai_client: OpenAI, llm_params: dict) -> Optional[str]:
        try:
            title_zh = self._generate_title_translation_with_llm(openai_client, llm_params)
            self.title_zh = title_zh
            return title_zh
        except Exception as e:
            logger.warning(f"Failed to translate title of {self.url}: {e}")
            self.title_zh = None
            return None

    def _generate_tldr_with_llm(self, openai_client:OpenAI,llm_params:dict) -> str:
        lang = llm_params.get('language', 'Chinese')
        prompt = (
            f"请阅读下面的论文，并严格按照下面的格式输出一份中文结构化摘要。\n"
            f"重要要求：\n"
            f"1. 必须使用中文撰写。涉及专业英文缩写时（如 RL、MPC、RAG、LVLM、GRPO、LLM 等），"
            f"保留英文缩写，但首次出现时用括号中文解释一次（例如：RL（强化学习））。\n"
            f"2. 每一段 1-2 句话，精炼直接，不要复述摘要原文。\n"
            f"3. 只输出三段结构化内容，不要任何前言、思考过程、格式说明或结束语。\n\n"
            f"必须严格使用如下三个中文小节标签（不要增删）：\n"
            f"【核心工作】<用 1-2 句中文描述论文的问题与方法>\n"
            f"【主要创新】<用 1-2 句中文描述关键技术贡献或新颖思想>\n"
            f"【潜在价值】<用 1-2 句中文描述实际影响、应用或研究价值>\n\n"
        )
        if self.title:
            prompt += f"Title:\n {self.title}\n\n"

        if self.abstract:
            prompt += f"Abstract: {self.abstract}\n\n"

        if self.full_text:
            prompt += f"Preview of main content:\n {self.full_text}\n\n"

        if not self.full_text and not self.abstract:
            logger.warning(f"Neither full text nor abstract is provided for {self.url}")
            return "Failed to generate TLDR. Neither full text nor abstract is provided"

        # use gpt-4o tokenizer for estimation
        enc = tiktoken.encoding_for_model("gpt-4o")
        prompt_tokens = enc.encode(prompt)
        prompt_tokens = prompt_tokens[:4000]  # truncate to 4000 tokens
        prompt = enc.decode(prompt_tokens)

        response = openai_client.chat.completions.create(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是一名资深 AI 研究者，负责为忙碌的读者总结科研论文。"
                        "必须使用中文撰写全部内容；只有在涉及已广泛使用的英文缩写（如 RL、MPC、RAG、LLM 等）时"
                        "才可保留英文，且首次出现需用括号中文解释一次。"
                        "严格遵循三段结构，不要输出任何思考过程、前言、方案说明或结束语；"
                        "不要英文句子，不要复述摘要原文；直接以【核心工作】开头。"
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            **llm_params.get('generation_kwargs', {})
        )
        tldr = response.choices[0].message.content or ""
        tldr = _clean_tldr(tldr)
        return tldr
    
    def generate_tldr(self, openai_client:OpenAI,llm_params:dict) -> str:
        try:
            tldr = self._generate_tldr_with_llm(openai_client,llm_params)
            self.tldr = tldr
            return tldr
        except Exception as e:
            logger.warning(f"Failed to generate tldr of {self.url}: {e}")
            tldr = self.abstract
            self.tldr = tldr
            return tldr

    def _generate_affiliations_with_llm(self, openai_client:OpenAI,llm_params:dict) -> Optional[list[str]]:
        if self.full_text is not None:
            prompt = f"Given the beginning of a paper, extract the affiliations of the authors in a python list format, which is sorted by the author order. If there is no affiliation found, return an empty list '[]':\n\n{self.full_text}"
            # use gpt-4o tokenizer for estimation
            enc = tiktoken.encoding_for_model("gpt-4o")
            prompt_tokens = enc.encode(prompt)
            prompt_tokens = prompt_tokens[:2000]  # truncate to 2000 tokens
            prompt = enc.decode(prompt_tokens)
            affiliations = openai_client.chat.completions.create(
                messages=[
                    {
                        "role": "system",
                        "content": "You are an assistant who perfectly extracts affiliations of authors from a paper. You should return a python list of affiliations sorted by the author order, like [\"TsingHua University\",\"Peking University\"]. If an affiliation is consisted of multi-level affiliations, like 'Department of Computer Science, TsingHua University', you should return the top-level affiliation 'TsingHua University' only. Do not contain duplicated affiliations. If there is no affiliation found, you should return an empty list [ ]. You should only return the final list of affiliations, and do not return any intermediate results.",
                    },
                    {"role": "user", "content": prompt},
                ],
                **llm_params.get('generation_kwargs', {})
            )
            affiliations = affiliations.choices[0].message.content

            affiliations = re.search(r'\[.*?\]', affiliations, flags=re.DOTALL).group(0)
            affiliations = json.loads(affiliations)
            affiliations = list(set(affiliations))
            affiliations = [str(a) for a in affiliations]

            return affiliations
    
    def generate_affiliations(self, openai_client:OpenAI,llm_params:dict) -> Optional[list[str]]:
        try:
            affiliations = self._generate_affiliations_with_llm(openai_client,llm_params)
            self.affiliations = affiliations
            return affiliations
        except Exception as e:
            logger.warning(f"Failed to generate affiliations of {self.url}: {e}")
            self.affiliations = None
            return None
@dataclass
class CorpusPaper:
    title: str
    abstract: str
    added_date: datetime
    paths: list[str]