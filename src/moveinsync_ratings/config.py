"""Load config.yaml, taxonomy.yaml and .env from the project root."""

from __future__ import annotations

import os
from functools import cached_property
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field


class AppRef(BaseModel):
    id: str
    name: str
    country: str = "in"
    lang: str = "en"


class AnalysisSettings(BaseModel):
    recent_days: int = 90
    comparison_days: int = 365
    min_reviews_per_version: int = 25


class LLMSettings(BaseModel):
    base_url: str
    api_key_env: str = "NVIDIA_API_KEY"
    # Models, best first. Chosen with `ratings eval`.
    models: list[str] = Field(min_length=1)
    # "fallback": the first model tags everything; later models only tag what it failed on.
    # "vote": every model tags every review; an issue counts when most of them agree.
    strategy: Literal["fallback", "vote"] = "fallback"
    batch_size: int = 20
    requests_per_minute: int = 35
    max_retries: int = 6
    timeout_s: int = 120

    @property
    def model(self) -> str:
        return self.models[0]

    @property
    def api_key(self) -> str | None:
        return os.environ.get(self.api_key_env) or None


class Paths(BaseModel):
    database: str = "data/ratings.db"
    reports: str = "reports"
    gold: str = "data/gold/reference_labels.jsonl"


class Settings(BaseModel):
    root: Path = Field(exclude=True)
    app: AppRef
    benchmarks: list[AppRef] = []
    sibling_apps: list[AppRef] = []
    tag_since: str = "2023-01-01"
    analysis: AnalysisSettings = AnalysisSettings()
    llm: LLMSettings
    paths: Paths = Paths()

    @cached_property
    def database(self) -> Path:
        return self.root / self.paths.database

    @cached_property
    def reports_dir(self) -> Path:
        return self.root / self.paths.reports

    @cached_property
    def gold_path(self) -> Path:
        return self.root / self.paths.gold


def find_root(start: Path | None = None) -> Path:
    """Walk up from `start` (default: cwd) to the folder holding config.yaml."""
    here = (start or Path.cwd()).resolve()
    for folder in (here, *here.parents):
        if (folder / "config.yaml").is_file() and (folder / "taxonomy.yaml").is_file():
            return folder
    # Fall back to the source checkout this package lives in.
    pkg_root = Path(__file__).resolve().parents[2]
    if (pkg_root / "config.yaml").is_file():
        return pkg_root
    raise FileNotFoundError("Could not find config.yaml. Run this from the project folder (the one with config.yaml).")


def load_settings(root: Path | None = None) -> Settings:
    root = root or find_root()
    load_dotenv(root / ".env")
    raw = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
    return Settings(root=root, **raw)
