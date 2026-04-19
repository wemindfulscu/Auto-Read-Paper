"""Shared stub factories for tests. No unittest.mock anywhere."""

from datetime import datetime
from types import SimpleNamespace

from auto_read_paper.protocol import CorpusPaper, Paper


# ---------------------------------------------------------------------------
# OpenAI client stub
# ---------------------------------------------------------------------------

_AFFILIATION_MARKER = "extracts affiliations"
_AFFILIATION_RESPONSE = '["TsingHua University","Peking University"]'
_TLDR_RESPONSE = "Hello! How can I assist you today?"


def _make_chat_response(content: str) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content),
                finish_reason="stop",
                index=0,
            )
        ],
        id="chatcmpl-stub",
        created=1765197615,
        model="gpt-4o-mini-2024-07-18",
        object="chat.completion",
    )


def _stub_chat_create(**kwargs):
    messages = kwargs.get("messages", [])
    request_str = str(messages)
    if _AFFILIATION_MARKER in request_str:
        return _make_chat_response(_AFFILIATION_RESPONSE)
    return _make_chat_response(_TLDR_RESPONSE)


def _stub_embeddings_create(**kwargs):
    inputs = kwargs.get("input", [])
    n = len(inputs) if isinstance(inputs, list) else 1
    return SimpleNamespace(
        data=[SimpleNamespace(embedding=[0.1, 0.2, 0.3], index=i, object="embedding") for i in range(n)],
        model="text-embedding-3-large",
        object="list",
    )


def make_stub_openai_client():
    """Return a SimpleNamespace that quacks like openai.OpenAI()."""
    return SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=_stub_chat_create),
        ),
        embeddings=SimpleNamespace(create=_stub_embeddings_create),
    )


# ---------------------------------------------------------------------------
# LLMClient stub (quacks like auto_read_paper.llm_client.LLMClient)
# ---------------------------------------------------------------------------


class StubLLMClient:
    """Minimal LLMClient-shaped object for tests.

    Routes prompts to canned responses based on the system message content:
      - if the system mentions affiliation extraction → affiliation array
      - otherwise → TLDR response
    Tests can override by passing a custom ``responses`` mapping of
    substring → response text, or ``raises`` to force an exception.
    """

    def __init__(self, responses: dict[str, str] | None = None, raises: BaseException | None = None):
        self.responses = responses or {}
        self.raises = raises
        self.calls: list[tuple[str, str]] = []

    def _pick(self, system: str, user: str) -> str:
        for marker, resp in self.responses.items():
            if marker in system or marker in user:
                return resp
        if _AFFILIATION_MARKER in system or _AFFILIATION_MARKER in user:
            return _AFFILIATION_RESPONSE
        return _TLDR_RESPONSE

    def complete(self, *, system: str, user: str, json_mode: bool = False) -> str:
        if self.raises is not None:
            raise self.raises
        self.calls.append((system, user))
        return self._pick(system, user)

    def complete_json(self, *, system: str, user: str, expect: str = "object"):
        import json as _json
        from auto_read_paper.llm_client import _extract_json_blob, _loads_tolerant

        raw = self.complete(system=system, user=user, json_mode=True)
        blob = _extract_json_blob(raw, expect=expect)
        if blob is None:
            other = "array" if expect == "object" else "object"
            blob = _extract_json_blob(raw, expect=other)
        if blob is None:
            return None
        try:
            return _loads_tolerant(blob)
        except _json.JSONDecodeError:
            return None

    def token_count(self, text: str) -> int:
        return max(1, len(text) // 4)

    def truncate_to_tokens(self, text: str, max_tokens: int) -> str:
        if not text or max_tokens <= 0:
            return ""
        return text[: max_tokens * 4]


def make_stub_llm_client(**kwargs) -> StubLLMClient:
    return StubLLMClient(**kwargs)


# ---------------------------------------------------------------------------
# SMTP stub
# ---------------------------------------------------------------------------


def make_stub_smtp(sent_emails: list):
    """Return a class that records calls to sendmail()."""

    class StubSMTP:
        def __init__(self, *args, **kwargs):
            pass

        def starttls(self):
            pass

        def login(self, user, password):
            pass

        def sendmail(self, sender, recipients, msg):
            sent_emails.append((sender, recipients, msg))

        def quit(self):
            pass

    return StubSMTP


# ---------------------------------------------------------------------------
# Paper / CorpusPaper factories
# ---------------------------------------------------------------------------


def make_sample_paper(**overrides) -> Paper:
    defaults = dict(
        source="arxiv",
        title="Sample Paper Title",
        authors=["Author A", "Author B", "Author C"],
        abstract="This paper explores a novel approach to widget engineering.",
        url="https://arxiv.org/abs/2026.00001",
        pdf_url="https://arxiv.org/pdf/2026.00001",
        full_text="\\begin{document} Some text. \\end{document}",
        tldr=None,
        affiliations=None,
        score=None,
    )
    defaults.update(overrides)
    return Paper(**defaults)


def make_sample_corpus(n: int = 3) -> list[CorpusPaper]:
    return [
        CorpusPaper(
            title=f"Corpus Paper {i}",
            abstract=f"Abstract for corpus paper {i}.",
            added_date=datetime(2026, 1, 1 + i),
            paths=[f"2026/survey/topic-{i}"],
        )
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# bioRxiv canned API response
# ---------------------------------------------------------------------------

SAMPLE_BIORXIV_API_RESPONSE = {
    "messages": [{"status": "ok"}],
    "collection": [
        {
            "doi": "10.1101/2026.03.01.000001",
            "title": "A biorxiv paper",
            "authors": "Smith, J.; Doe, A.; Lee, K.",
            "abstract": "We present a novel finding.",
            "date": "2026-03-02",
            "category": "bioinformatics",
            "version": "1",
        },
        {
            "doi": "10.1101/2026.03.01.000002",
            "title": "Another biorxiv paper",
            "authors": "Wang, L.; Chen, M.",
            "abstract": "We replicate a key result.",
            "date": "2026-03-02",
            "category": "genomics",
            "version": "1",
        },
        {
            "doi": "10.1101/2026.03.01.000003",
            "title": "Old biorxiv paper",
            "authors": "Old, R.",
            "abstract": "Yesterday's paper.",
            "date": "2026-03-01",
            "category": "bioinformatics",
            "version": "1",
        },
    ],
}


# ---------------------------------------------------------------------------
# SMTP stub
# ---------------------------------------------------------------------------


def make_stub_smtp(sent_emails: list):
    """Return a class that records calls to sendmail().

    Usage:
        sent = []
        monkeypatch.setattr(smtplib, "SMTP", make_stub_smtp(sent))
        ...
        assert len(sent) == 1
        sender, recipients, body = sent[0]
    """

    class StubSMTP:
        def __init__(self, *args, **kwargs):
            pass

        def starttls(self):
            pass

        def login(self, user, password):
            pass

        def sendmail(self, sender, recipients, msg):
            sent_emails.append((sender, recipients, msg))

        def quit(self):
            pass

    return StubSMTP


# ---------------------------------------------------------------------------
# Paper / CorpusPaper factories
# ---------------------------------------------------------------------------


def make_sample_paper(**overrides) -> Paper:
    defaults = dict(
        source="arxiv",
        title="Sample Paper Title",
        authors=["Author A", "Author B", "Author C"],
        abstract="This paper explores a novel approach to widget engineering.",
        url="https://arxiv.org/abs/2026.00001",
        pdf_url="https://arxiv.org/pdf/2026.00001",
        full_text="\\begin{document} Some text. \\end{document}",
        tldr=None,
        affiliations=None,
        score=None,
    )
    defaults.update(overrides)
    return Paper(**defaults)


def make_sample_corpus(n: int = 3) -> list[CorpusPaper]:
    return [
        CorpusPaper(
            title=f"Corpus Paper {i}",
            abstract=f"Abstract for corpus paper {i}.",
            added_date=datetime(2026, 1, 1 + i),
            paths=[f"2026/survey/topic-{i}"],
        )
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# bioRxiv canned API response
# ---------------------------------------------------------------------------

SAMPLE_BIORXIV_API_RESPONSE = {
    "messages": [{"status": "ok"}],
    "collection": [
        {
            "doi": "10.1101/2026.03.01.000001",
            "title": "A biorxiv paper",
            "authors": "Smith, J.; Doe, A.; Lee, K.",
            "abstract": "We present a novel finding.",
            "date": "2026-03-02",
            "category": "bioinformatics",
            "version": "1",
        },
        {
            "doi": "10.1101/2026.03.01.000002",
            "title": "Another biorxiv paper",
            "authors": "Wang, L.; Chen, M.",
            "abstract": "We replicate a key result.",
            "date": "2026-03-02",
            "category": "genomics",
            "version": "1",
        },
        {
            "doi": "10.1101/2026.03.01.000003",
            "title": "Old biorxiv paper",
            "authors": "Old, R.",
            "abstract": "Yesterday's paper.",
            "date": "2026-03-01",
            "category": "bioinformatics",
            "version": "1",
        },
    ],
}
