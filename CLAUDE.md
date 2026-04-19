# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Auto-Read-Paper fetches newly-announced arXiv papers daily, runs a keyword pre-filter + multi-agent (Reader + Reviewer) LLM rerank, generates localized three-section deep-read summaries, and delivers an HTML digest by email. Runs entirely on GitHub Actions at zero cost.

## Commands

```bash
# Run the application
uv run src/auto_read_paper/main.py

# Run tests (excludes slow tests by default)
uv run pytest

# Run all tests including slow ones
uv run pytest -m ""

# Run a single test
uv run pytest tests/test_utils.py::TestGlobMatch -v

# Install/sync dependencies
uv sync
```

No linter or formatter is configured.

## Architecture

The app follows a linear pipeline orchestrated by `Executor` (`src/auto_read_paper/executor.py`):

1. **Retrieve** — pulls newly-announced arXiv papers in the configured categories via the RSS feed; arXiv-native author affiliations are fetched in the same pass
2. **Keyword pre-filter** — drops papers whose title/abstract doesn't match any configured keyword (before any LLM call)
3. **Rerank** — either `reader_reviewer` (default: Reader extracts per-paper structured notes, Reviewer batch-ranks them) or `keyword_llm` (per-paper LLM scoring)
4. **History merge** — today's scored papers merge with the past-N-days unsent pool from `state/score_history.json`
5. **Deep read** — the Top-N are sent back to the LLM for a three-section localized summary (`[CORE]` / `[INNOVATION]` / `[VALUE]`) and, if needed, a translated title
6. **Render + send email** — HTML template rendered and sent via SMTP; papers are only marked sent after SMTP succeeds

### Plugin Systems

**Retrievers** (`src/auto_read_paper/retriever/`): Register via `@register_retriever` decorator, discovered by `get_retriever_cls()`. Each retriever implements `_retrieve_raw_papers()` and `convert_to_paper()`.

**Rerankers** (`src/auto_read_paper/reranker/`): Register via `@register_reranker` decorator, discovered by `get_reranker_cls()`. Two implementations: `keyword_llm` (per-paper LLM scoring) and `reader_reviewer` (two-agent pipeline, default).

### LLM Gateway

All LLM traffic goes through `LLMClient` in [src/auto_read_paper/llm_client.py](src/auto_read_paper/llm_client.py), a thin wrapper around LiteLLM. Never call `openai.OpenAI` or `litellm.completion` directly from feature code. The client handles:

- Provider routing via LiteLLM model prefixes (`openai/`, `anthropic/`, `gemini/`, `deepseek/`, `ollama/`, `openrouter/`, ...).
- Reasoning-model param translation (o1 / o3 / o4 / gpt-5: `max_tokens` → `max_completion_tokens`, drop `temperature` / `top_p`).
- `response_format={"type":"json_object"}` only when the provider whitelist supports it.
- Balanced-brace JSON extraction in `complete_json` that survives markdown fences, `<think>` blocks, single-quote Python-style dicts, and prose preambles.
- Default `timeout=60`, `num_retries=3`.

Construct one `LLMClient` per consumer via `LLMClient.from_config(config.llm)` and reuse it.

### Configuration

Uses Hydra + OmegaConf. Config is composed from `config/base.yaml` (defaults) + `config/custom.yaml` (user overrides). Environment variables are interpolated via `${oc.env:VAR_NAME,default}` syntax. Entry point uses `@hydra.main`.

Public env vars are `LLM_API_KEY` / `LLM_API_BASE` / `LLM_MODEL` / `LLM_MAX_TOKENS`. The legacy `OPENAI_*` names are honored as a deprecation-shim fallback so pre-rename forks keep running — use of the legacy name emits a workflow `::warning::` and a `loguru` warning. The legacy nested `llm.generation_kwargs.{model,max_tokens,...}` block is similarly accepted with a deprecation warning. When renaming any user-facing env var or config key in the future, follow the same two-speed policy (hard rename internals, compat shim public names).

### Data Classes

`Paper` in [src/auto_read_paper/protocol.py](src/auto_read_paper/protocol.py). `Paper.generate_tldr` / `generate_title_zh` / `generate_affiliations` take an `LLMClient` instance — they do not talk to any provider SDK directly.

## Testing

Tests marked `@pytest.mark.slow` require heavy dependencies (e.g., sentence-transformers model download) and are skipped locally by default (`addopts = "-m 'not slow'"` in pyproject.toml). All other tests run with pure Python stubs (no Docker containers needed).

```bash
# Run tests (excludes slow tests)
uv run pytest

# Run all tests including slow ones
uv run pytest -m ""

# Run with coverage
uv run pytest --cov=src/auto_read_paper --cov-report=term-missing
```

## gstack

Use the `/browse` skill from gstack for all web browsing. Never use `mcp__claude-in-chrome__*` tools.

Available skills: `/office-hours`, `/plan-ceo-review`, `/plan-eng-review`, `/plan-design-review`, `/design-consultation`, `/design-shotgun`, `/design-html`, `/review`, `/ship`, `/land-and-deploy`, `/canary`, `/benchmark`, `/browse`, `/connect-chrome`, `/qa`, `/qa-only`, `/design-review`, `/setup-browser-cookies`, `/setup-deploy`, `/retro`, `/investigate`, `/document-release`, `/codex`, `/cso`, `/autoplan`, `/plan-devex-review`, `/devex-review`, `/careful`, `/freeze`, `/guard`, `/unfreeze`, `/gstack-upgrade`, `/learn`.

If gstack skills aren't working, run `cd .claude/skills/gstack && ./setup` to build the binary and register skills.

## Git Workflow

- PRs should target the `dev` branch, not `main`
- Current development branch: `dev`
