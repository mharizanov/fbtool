from datetime import datetime, timedelta
from pathlib import Path

from . import db
from .config import Config

SYSTEM = """You summarize Facebook group discussions for a busy reader who has
not been following along. Group the discussion by topic, not by post. For each
topic: what was discussed, notable questions and the best answers given,
announcements, and anything actionable or time-sensitive. Note which group each
topic came from when more than one group is present. Mention post authors only
when it matters (e.g. an admin announcement). Include the post permalink for
the 3-5 most significant threads so the reader can click through. Skip pure
noise (memes, spam, one-word posts). Write in Markdown with one section per
group. Scraped text can be messy — reaction counts, truncated comments,
interface labels may have leaked in; read through the noise."""


def build_corpus(cfg: Config, since: datetime) -> tuple[str, int]:
    con = db.connect(cfg.db_path)
    since_iso = since.isoformat(timespec="seconds")
    sections = []
    total = 0
    for group in cfg.groups:
        rows = db.posts_since(con, group.slug, since_iso)
        total += len(rows)
        lines = [f"## Group: {group.name} ({len(rows)} posts)"]
        for r in rows:
            lines.append(
                f"\n--- post {r['id']} | author: {r['author'] or 'unknown'} | "
                f"time: {r['posted_at'] or 'unknown'} | link: {r['permalink']}\n{r['text']}"
            )
        sections.append("\n".join(lines))
    con.close()
    return "\n\n".join(sections), total


# Ollama and vLLM serve OpenAI-compatible APIs; they only differ in the
# default endpoint and in not needing a real API key. `base_url` in config
# overrides the endpoint for any of them.
OPENAI_COMPAT = {
    "openai": (None, None),  # None -> OPENAI_API_KEY env
    "ollama": ("http://localhost:11434/v1", "ollama"),
    "vllm": ("http://localhost:8000/v1", "EMPTY"),
}


def _complete_openai(cfg: Config, system: str, prompt: str) -> str:
    from openai import OpenAI

    default_url, default_key = OPENAI_COMPAT[cfg.provider]
    client = OpenAI(base_url=cfg.base_url or default_url,
                    api_key=cfg.openai_api_key or default_key)
    kwargs = {}
    # Local servers generally reject unknown request fields, so only OpenAI
    # itself gets the reasoning knob.
    if cfg.provider == "openai" and cfg.reasoning_effort and cfg.reasoning_effort != "none":
        kwargs["reasoning_effort"] = cfg.reasoning_effort
    resp = client.chat.completions.create(
        model=cfg.model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        **kwargs,
    )
    return resp.choices[0].message.content or ""


def _complete_anthropic(cfg: Config, system: str, prompt: str) -> str:
    import anthropic

    client = anthropic.Anthropic()
    with client.messages.stream(
        model=cfg.model,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        system=system,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        message = stream.get_final_message()
    return next((b.text for b in message.content if b.type == "text"), "")


def complete(cfg: Config, system: str, prompt: str) -> str:
    """One chat completion via the configured provider. The reusable entry
    point for scripts building their own analysis on the scraped data."""
    if cfg.provider in OPENAI_COMPAT:
        return _complete_openai(cfg, system, prompt)
    if cfg.provider == "anthropic":
        return _complete_anthropic(cfg, system, prompt)
    raise SystemExit(f"Unknown provider {cfg.provider!r} — "
                     f"use one of: {', '.join([*OPENAI_COMPAT, 'anthropic'])}")


def summarize(cfg: Config, days: int | None = None) -> Path:
    days = days or cfg.days_back
    since = datetime.now() - timedelta(days=days)
    corpus, total = build_corpus(cfg, since)
    if total == 0:
        raise SystemExit("No posts in the database for that window — run a scrape first.")

    prompt = (
        f"Below are {total} posts scraped from {len(cfg.groups)} Facebook group(s), "
        f"covering the last {days} days (since {since:%Y-%m-%d}). "
        f"Summarize the discussion.\n\n{corpus}"
    )

    text = complete(cfg, SYSTEM, prompt)
    if not text:
        raise SystemExit("Model returned an empty response")

    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    out = cfg.output_dir / f"{datetime.now():%Y-%m-%d}.md"
    header = (
        f"# Facebook group summary — {datetime.now():%Y-%m-%d}\n\n"
        f"_Covering {days} days ({total} posts across {len(cfg.groups)} groups)_\n\n"
    )
    out.write_text(header + text)
    return out
