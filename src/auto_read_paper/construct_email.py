from .protocol import Paper
import html as _html
import math
import re


framework = """
<!DOCTYPE HTML>
<html>
<head>
  <meta charset="UTF-8">
  <style>
    body { background: #f3f4f6; margin: 0; padding: 24px 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", Arial, sans-serif; }
    .container { max-width: 760px; margin: 0 auto; padding: 0 12px; }
    .digest-header { text-align: center; padding: 20px 16px; background: linear-gradient(135deg, #4f46e5 0%, #7c3aed 100%); border-radius: 12px; color: #fff; margin-bottom: 20px; }
    .digest-header h1 { margin: 0; font-size: 22px; letter-spacing: 0.5px; }
    .digest-header .sub { margin-top: 6px; font-size: 13px; opacity: 0.9; }
    .paper-card { background: #ffffff; border: 1px solid #e5e7eb; border-left: 5px solid #4f46e5; border-radius: 10px; padding: 18px 20px; margin-bottom: 18px; box-shadow: 0 1px 2px rgba(0,0,0,0.04); }
    .paper-title-en { font-size: 17px; font-weight: 700; color: #111827; line-height: 1.4; margin: 0 0 4px 0; }
    .paper-title-zh { font-size: 14px; font-weight: 500; color: #4b5563; line-height: 1.4; margin: 0 0 10px 0; }
    .paper-meta { font-size: 13px; color: #6b7280; line-height: 1.55; margin-bottom: 10px; }
    .paper-meta .aff { color: #9ca3af; font-style: italic; }
    .score-badge { display: inline-block; background: #eef2ff; color: #4338ca; font-size: 12px; font-weight: 600; padding: 3px 10px; border-radius: 999px; margin-bottom: 12px; }
    .section { margin: 8px 0; font-size: 14px; line-height: 1.65; color: #1f2937; }
    .section-label { display: inline-block; font-weight: 700; color: #fff; padding: 2px 8px; border-radius: 4px; margin-right: 6px; font-size: 12px; letter-spacing: 0.3px; }
    .label-core { background: #2563eb; }
    .label-novel { background: #059669; }
    .label-value { background: #d97706; }
    .pdf-btn { display: inline-block; text-decoration: none; font-size: 13px; font-weight: 600; color: #fff; background: #dc2626; padding: 7px 16px; border-radius: 6px; margin-top: 10px; }
    .pdf-btn:hover { background: #b91c1c; }
    .footer { text-align: center; font-size: 12px; color: #9ca3af; margin-top: 20px; padding: 16px; }
  </style>
</head>
<body>
<div class="container">
    __CONTENT__
    <div class="footer">
        To unsubscribe, remove your email in your Github Action setting.
    </div>
</div>
</body>
</html>
"""


def get_empty_html():
    return """
    <div class="paper-card" style="text-align:center;">
        <div class="paper-title-en">No Papers Today. Take a Rest! 🌿</div>
        <div class="paper-title-zh">今日暂无新论文，休息一下吧</div>
    </div>
    """


_SECTION_STYLES = {
    "【核心工作】": ("label-core", "核心工作"),
    "【主要创新】": ("label-novel", "主要创新"),
    "【潜在价值】": ("label-value", "潜在价值"),
}


def _format_tldr(tldr: str) -> str:
    """Turn the raw 【核心工作】/【主要创新】/【潜在价值】 block into styled HTML.

    Falls back to showing the text as-is (with line breaks) when the labels
    can't be parsed.
    """
    if not tldr:
        return ""
    text = tldr.replace("<br>", "\n").strip()

    parts = re.split(r"(【核心工作】|【主要创新】|【潜在价值】)", text)
    if len(parts) <= 1:
        return f'<div class="section">{_html.escape(text).replace(chr(10), "<br>")}</div>'

    out = []
    current_label = None
    for chunk in parts:
        if chunk in _SECTION_STYLES:
            current_label = chunk
            continue
        content = chunk.strip(" \n:：")
        if not content:
            continue
        if current_label and current_label in _SECTION_STYLES:
            css, zh = _SECTION_STYLES[current_label]
            safe = _html.escape(content).replace("\n", "<br>")
            out.append(
                f'<div class="section"><span class="section-label {css}">{zh}</span>{safe}</div>'
            )
            current_label = None
    if not out:
        return f'<div class="section">{_html.escape(text).replace(chr(10), "<br>")}</div>'
    return "".join(out)


def get_block_html(title_en: str, title_zh: str, authors: str, rate, tldr: str, pdf_url: str, affiliations: str = None):
    title_zh_html = (
        f'<div class="paper-title-zh">{_html.escape(title_zh)}</div>'
        if title_zh else ""
    )
    aff_html = f'<span class="aff">{_html.escape(affiliations or "")}</span>' if affiliations else ""
    rate_html = f'<div class="score-badge">⭐ Relevance {rate}</div>' if rate != "Unknown" else ""
    return f"""
    <div class="paper-card">
        <div class="paper-title-en">{_html.escape(title_en)}</div>
        {title_zh_html}
        <div class="paper-meta">{_html.escape(authors)}<br>{aff_html}</div>
        {rate_html}
        {_format_tldr(tldr)}
        <a href="{pdf_url}" class="pdf-btn">📄 PDF</a>
    </div>
    """


def get_stars(score: float):
    full_star = '<span class="full-star">⭐</span>'
    half_star = '<span class="half-star">⭐</span>'
    low = 6
    high = 8
    if score <= low:
        return ''
    elif score >= high:
        return full_star * 5
    interval = (high - low) / 10
    star_num = math.ceil((score - low) / interval)
    full_star_num = int(star_num / 2)
    half_star_num = star_num - full_star_num * 2
    return '<div class="star-wrapper">' + full_star * full_star_num + half_star * half_star_num + '</div>'


def render_email(papers: list[Paper]) -> str:
    header = (
        '<div class="digest-header">'
        '<h1>📚 今日论文速递 · Auto-Read-Paper</h1>'
        '<div class="sub">多智能体阅读 · 分级评分 · 中文 AI 解读</div>'
        '</div>'
    )

    if len(papers) == 0:
        return framework.replace("__CONTENT__", header + get_empty_html())

    parts = [header]
    for p in papers:
        rate = round(p.score, 1) if p.score is not None else "Unknown"
        author_list = [a for a in p.authors]
        num_authors = len(author_list)
        if num_authors <= 5:
            authors = ", ".join(author_list)
        else:
            authors = ", ".join(author_list[:3] + ["..."] + author_list[-2:])
        if p.affiliations is not None and len(p.affiliations) > 0:
            affs = p.affiliations[:5]
            affiliations = ", ".join(affs)
            if len(p.affiliations) > 5:
                affiliations += ", ..."
        else:
            affiliations = "Unknown Affiliation"
        parts.append(
            get_block_html(
                p.title,
                getattr(p, "title_zh", None) or "",
                authors,
                rate,
                p.tldr or "",
                p.pdf_url,
                affiliations,
            )
        )

    return framework.replace("__CONTENT__", "\n".join(parts))
