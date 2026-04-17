<p align="center">
  <a href="" rel="noopener">
 <img width=200px height=200px src="assets/logo.svg" alt="logo"></a>
</p>

<h3 align="center">Auto-Read-Paper</h3>

<div align="center">

  [![Status](https://img.shields.io/badge/status-active-success.svg)]()

</div>

---

<p align="center">Fetch fresh arXiv papers every day, let an LLM read and rank them by relevance and core value, and email you a digest of the most valuable ones — fully automated on GitHub Actions.
    <br>
</p>

## 🧐 About

*Auto-Read-Paper* pulls newly-announced arXiv papers every day, filters by your keywords, has an LLM rate each on **innovation / relevance / potential**, then writes a structured Chinese AI summary for the top-N, and mails the result to your inbox. Runs on GitHub Actions with **zero infrastructure cost**.

No reading list, no local machine, no Zotero. Just keywords.

## ✨ Features

- **Fully automated** — daily cron on GitHub Actions, no server needed.
- **Beijing-time schedule** — set a single repo variable `SEND_HOUR_BJ` (0-23); the email lands at that Beijing hour every day.
- **Keyword-driven** — papers not matching your keywords are dropped before any LLM call (saves tokens).
- **LLM-graded ranking** — each candidate rated on innovation / relevance / potential (0-10 each), ranked by weighted composite.
- **Chinese structured summary** — each paper in the email gets a three-section AI breakdown: 核心工作 / 主要创新 / 潜在价值.
- **HTML email delivery** — clean paper cards with score, authors, affiliations, PDF link (same layout as upstream zotero-arxiv-daily).
- **Full-text aware** — extracts TeX / HTML / PDF to feed the LLM, not just the abstract.

## 📷 Screenshot

![screenshot](./assets/screenshot.png)

## 🚀 Usage

### Quick Start

1. **Fork this repo into your own GitHub account.**
   ![fork](./assets/fork.png)

2. **Set GitHub Action repository secrets.** They are invisible after saving, even to you.

   > **About Secrets vs Variables.** GitHub Actions exposes two kinds of repo-level configuration:
   > - **Secrets** (`${{ secrets.X }}`): encrypted, masked as `***` in logs, never readable after save. Use these for **anything sensitive** — passwords, API keys, SMTP auth codes.
   > - **Variables** (`${{ vars.X }}`): plain-text, visible in logs, editable any time. Use these for **non-sensitive config** — model id, schedule hour, feature toggles.
   >
   > Both live under repo **Settings → Secrets and variables → Actions** but in *separate tabs*. Neither is inherited when someone forks — every fork must set its own.

   ![secrets](./assets/secrets.png)

   | Key | Description | Example |
   | :--- | :--- | :--- |
   | `SENDER` | SMTP sender account. | `abc@qq.com` |
   | `SENDER_PASSWORD` | SMTP **auth code** (NOT your web login password — ask your email provider). | `abcdefghijklmn` |
   | `RECEIVER` | Inbox that receives the paper digest. | `abc@outlook.com` |
   | `OPENAI_API_KEY` | API key for the LLM. Any OpenAI-compatible provider works (OpenAI, DeepSeek, SiliconFlow, Qwen, etc.). | `sk-xxx` |
   | `OPENAI_API_BASE` | Base URL of the LLM API. | `https://api.openai.com/v1` |

3. **Set GitHub Action repository variables** (Variables tab, *not* Secrets).
   ![vars](./assets/repo_var.png)

   | Variable | Description | Example |
   | :--- | :--- | :--- |
   | `SEND_HOUR_BJ` | Beijing hour (0-23) at which the daily email is sent. Default `7`. | `7` |
   | `OPENAI_MODEL` | LLM model id used for both scoring and the deep-read summary. Any model your `OPENAI_API_BASE` provider serves. Default `gpt-4o-mini`. | `gpt-4o-mini`, `deepseek-chat`, `Qwen/Qwen2.5-72B-Instruct` |
   | `CUSTOM_CONFIG` | The full YAML configuration (see below). | *(multi-line YAML)* |

   ![custom_config](./assets/config_var.png)

   Paste the following into `CUSTOM_CONFIG`, then edit `keywords` / `category` / `model` to your taste:

   ```yaml
   email:
     sender: ${oc.env:SENDER}
     receiver: ${oc.env:RECEIVER}
     smtp_server: smtp.qq.com              # Your email provider's SMTP server
     smtp_port: 465
     sender_password: ${oc.env:SENDER_PASSWORD}

   llm:
     api:
       key: ${oc.env:OPENAI_API_KEY}
       base_url: ${oc.env:OPENAI_API_BASE}
     generation_kwargs:
       model: ${oc.env:OPENAI_MODEL,gpt-4o-mini}  # Picks up the OPENAI_MODEL repo variable
     language: Chinese

   source:
     arxiv:
       category: ["cs.AI","cs.LG","cs.RO"] # Coarse arXiv category filter
       include_cross_list: true
       keywords:                            # Fine-grained keyword filter (case-insensitive)
         - "reinforcement learning"
         - "model predictive control"
         - "residual policy"

   executor:
     debug: ${oc.env:DEBUG,null}
     send_empty: false
     max_paper_num: 10                     # Top-N papers shown in the email
     source: ['arxiv']
     reranker: keyword_llm
   ```

   > `${oc.env:XXX,yyy}` resolves to environment variable `XXX`, falling back to `yyy` when unset.

4. **Trigger the workflow manually to test it.**
   ![test](./assets/test.png)

   Check the workflow log and your inbox. After the test, the workflow also runs automatically — the job wakes up every hour, but only sends an email when the **Beijing hour equals `SEND_HOUR_BJ`** (default 07:00 Beijing time). Change the variable anytime to reschedule; no YAML edit needed.

### Full configuration reference

See [config/base.yaml](config/base.yaml) for every available knob, including:
- `reranker.keyword_llm.weights` — reweight innovation/relevance/potential.
- `reranker.keyword_llm.threshold` — drop papers below a minimum composite score.
- `reranker.keyword_llm.concurrency` — parallel LLM scoring requests.
- `source.arxiv.include_cross_list` — include cross-listed papers.
- `executor.send_empty` — still send the email even when no paper matched.

### Local Running

Powered by [uv](https://github.com/astral-sh/uv):
```bash
# export SENDER=... SENDER_PASSWORD=... RECEIVER=...
# export OPENAI_API_KEY=... OPENAI_API_BASE=...
cd Auto-Read-Paper
uv sync
DEBUG=true uv run src/auto_read_paper/main.py
```

## 📖 How it works

1. **Retrieve** — arXiv RSS feed gives today's newly-announced papers in the configured categories.
2. **Keyword pre-filter** — papers whose title or abstract doesn't mention any of your keywords are dropped (saves LLM cost).
3. **LLM scoring** — each surviving paper is rated on innovation / relevance / potential (0-10 each) and ranked by the weighted composite.
4. **Deep read** — top-N papers are fed (title + abstract + extracted full text) back to the LLM to produce a structured Chinese summary: 核心工作 / 主要创新 / 潜在价值.
5. **Email** — rendered as an HTML message via SMTP.

## 📌 Limitations

- arXiv RSS is the only source. Google Scholar has no stable API and would not survive on GitHub Actions runners.
- The LLM scoring is only as good as the prompt + model; for niche domains, expect some noise. Raise `max_paper_num` or tune `weights` to taste.
- GitHub Actions has a per-repo quota (6 h/run, 2000 min/month for private repos). Hourly wake-ups + one real daily run comfortably fit for public repos.

## 📃 License

Distributed under the AGPLv3 License. See `LICENSE` for detail.

## ❤️ Acknowledgement

This project stands on the shoulders of two open-source projects:

- [**TideDra/zotero-arxiv-daily**](https://github.com/TideDra/zotero-arxiv-daily) — the GitHub Actions + SMTP + HTML email foundation that this repo forks and extends.
- [**ReadPaperEveryday**](https://github.com/) — inspired the keyword-based arXiv workflow and Chinese deep-read summarization style.

Additional thanks to:
- [arxiv](https://github.com/lukasschwab/arxiv.py)
- [trafilatura](https://github.com/adbar/trafilatura)
- [pymupdf4llm](https://github.com/pymupdf/PyMuPDF)
