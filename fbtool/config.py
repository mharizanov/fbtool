from dataclasses import dataclass, field
from pathlib import Path

import yaml

PROJECT_DIR = Path(__file__).resolve().parent.parent


@dataclass
class Group:
    slug: str
    name: str


@dataclass
class Config:
    groups: list[Group]
    days_back: int = 7
    headless: bool = False
    max_scrolls: int = 40
    scroll_pause_ms: int = 2500
    provider: str = "openai"
    model: str = "gpt-5.5"
    reasoning_effort: str = "medium"
    openai_api_key: str | None = None
    profile_dir: Path = field(default=PROJECT_DIR / "profile")
    db_path: Path = field(default=PROJECT_DIR / "fbtool.db")
    output_dir: Path = field(default=PROJECT_DIR / "summaries")


def load(path: Path | None = None) -> Config:
    path = path or PROJECT_DIR / "config.yaml"
    raw = yaml.safe_load(path.read_text())

    groups = [Group(slug=str(g["slug"]), name=g.get("name", str(g["slug"])))
              for g in raw.get("groups", [])]
    groups = [g for g in groups if g.slug and g.slug != "REPLACE_ME"]

    def as_path(key: str, default: Path) -> Path:
        if key not in raw:
            return default
        p = Path(raw[key])
        return p if p.is_absolute() else PROJECT_DIR / p

    return Config(
        groups=groups,
        days_back=int(raw.get("days_back", 7)),
        headless=bool(raw.get("headless", False)),
        max_scrolls=int(raw.get("max_scrolls", 40)),
        scroll_pause_ms=int(raw.get("scroll_pause_ms", 2500)),
        provider=raw.get("provider", "openai"),
        model=raw.get("model", "gpt-5.5"),
        reasoning_effort=raw.get("reasoning_effort", "medium"),
        openai_api_key=raw.get("openai_api_key"),
        profile_dir=as_path("profile_dir", PROJECT_DIR / "profile"),
        db_path=as_path("db_path", PROJECT_DIR / "fbtool.db"),
        output_dir=as_path("output_dir", PROJECT_DIR / "summaries"),
    )
