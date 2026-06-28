"""Configuration loading: env > config.toml > defaults.

Secrets (tokens, API keys) are never logged. The pydantic models redact
sensitive fields in their repr so accidental logging stays safe.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

try:  # Python 3.11+
    import tomllib  # type: ignore[attr-defined]
except ModuleNotFoundError:  # pragma: no cover - exercised on <3.11
    import tomli as tomllib  # type: ignore[no-redef]


class GithubConfig(BaseModel):
    token: str = ""
    repo: str = "tianocore/edk2"

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"GithubConfig(repo={self.repo!r}, token={'set' if self.token else 'unset'})"


class LLMConfig(BaseModel):
    provider: str = "openai"
    api_key: str = ""
    model: str = "gpt-4o"
    base_url: str | None = None
    temperature: float = 0.2

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (
            f"LLMConfig(provider={self.provider!r}, model={self.model!r}, "
            f"api_key={'set' if self.api_key else 'unset'})"
        )


class StorageConfig(BaseModel):
    db_path: str = "/data/llm-summary.sqlite"
    site_dir: str = "/site"


class CrawlerConfig(BaseModel):
    timezone: str = "UTC"
    default_bootstrap_days: int = 1


class ProxyConfig(BaseModel):
    # A single proxy URL applied to all outbound traffic (GitHub + LLM).
    # Supports http(s):// and socks5(h):// schemes, e.g.
    #   "socks5h://user:pass@proxy.corp:1080"
    url: str = ""


class Config(BaseModel):
    github: GithubConfig = Field(default_factory=GithubConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    crawler: CrawlerConfig = Field(default_factory=CrawlerConfig)
    proxy: ProxyConfig = Field(default_factory=ProxyConfig)


def _load_toml(path: str | os.PathLike[str] | None) -> dict[str, Any]:
    if not path:
        return {}
    p = Path(path)
    if not p.is_file():
        return {}
    with p.open("rb") as fh:
        return tomllib.load(fh)


def _apply_env_overrides(data: dict[str, Any]) -> dict[str, Any]:
    """Apply environment overrides. Env wins over toml.

    Recognised variables (in addition to the path-related ones handled in
    load_config): GITHUB_TOKEN, GITHUB_REPO, LLM_API_KEY, LLM_PROVIDER,
    LLM_MODEL, LLM_BASE_URL, LLM_TEMPERATURE, CRAWLER_TIMEZONE,
    CRAWLER_DEFAULT_BOOTSTRAP_DAYS.
    """
    github = data.setdefault("github", {})
    llm = data.setdefault("llm", {})
    storage = data.setdefault("storage", {})
    crawler = data.setdefault("crawler", {})
    proxy = data.setdefault("proxy", {})

    env = os.environ
    if "GITHUB_TOKEN" in env:
        github["token"] = env["GITHUB_TOKEN"]
    if "GITHUB_REPO" in env:
        github["repo"] = env["GITHUB_REPO"]

    if "LLM_API_KEY" in env:
        llm["api_key"] = env["LLM_API_KEY"]
    # Allow the common OPENAI_API_KEY as a fallback for the OpenAI provider.
    elif "OPENAI_API_KEY" in env and not llm.get("api_key"):
        llm["api_key"] = env["OPENAI_API_KEY"]
    if "LLM_PROVIDER" in env:
        llm["provider"] = env["LLM_PROVIDER"]
    if "LLM_MODEL" in env:
        llm["model"] = env["LLM_MODEL"]
    if env.get("LLM_BASE_URL"):
        llm["base_url"] = env["LLM_BASE_URL"]
    if "LLM_TEMPERATURE" in env:
        llm["temperature"] = float(env["LLM_TEMPERATURE"])

    if "LLM_SUMMARY_DB" in env:
        storage["db_path"] = env["LLM_SUMMARY_DB"]
    if "LLM_SUMMARY_SITE" in env:
        storage["site_dir"] = env["LLM_SUMMARY_SITE"]

    if "CRAWLER_TIMEZONE" in env:
        crawler["timezone"] = env["CRAWLER_TIMEZONE"]
    if "CRAWLER_DEFAULT_BOOTSTRAP_DAYS" in env:
        crawler["default_bootstrap_days"] = int(env["CRAWLER_DEFAULT_BOOTSTRAP_DAYS"])

    # Proxy: explicit LLM_SUMMARY_PROXY wins; otherwise fall back to the
    # conventional ALL_PROXY / HTTPS_PROXY / HTTP_PROXY env vars so corporate
    # setups that only export the standard variables still work.
    for key in ("LLM_SUMMARY_PROXY", "ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        if env.get(key):
            proxy["url"] = env[key]
            break

    return data


def load_config(config_path: str | os.PathLike[str] | None = None) -> Config:
    """Load configuration with priority env > config.toml > defaults.

    config_path defaults to the LLM_SUMMARY_CONFIG env var. A missing file is
    fine; the app runs from env + defaults.
    """
    if config_path is None:
        config_path = os.environ.get("LLM_SUMMARY_CONFIG")
    data = _load_toml(config_path)
    data = _apply_env_overrides(data)
    return Config.model_validate(data)


def base_dir() -> Path:
    """Repo/app base dir holding templates/ and assets/.

    Honours LLM_SUMMARY_BASE_DIR (set in the container); otherwise resolves
    relative to this file (src/llm_summary/config.py -> repo root).
    """
    env = os.environ.get("LLM_SUMMARY_BASE_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2]
