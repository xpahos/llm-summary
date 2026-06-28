"""Proxy support for outbound traffic (GitHub via requests, LLM via httpx).

A single configured proxy URL is normalized into the scheme each client expects:

* requests (PyGithub) reads HTTP(S)_PROXY / ALL_PROXY from the environment and
  distinguishes ``socks5`` (client-side DNS) from ``socks5h`` (proxy-side DNS).
  Corporate proxies almost always require proxy-side DNS, so we force ``socks5h``.
* httpx (openai/langchain) takes an explicit ``proxy=`` argument, only understands
  the ``socks5`` scheme, and already resolves names on the proxy. We pass it a
  client with ``trust_env=False`` so it ignores the ``socks5h`` env we set for
  requests (which httpx would reject).

SOCKS support requires the optional deps: ``PySocks`` (requests) and
``httpx[socks]`` (httpx); both are declared in pyproject.
"""

from __future__ import annotations

import logging
import os
from urllib.parse import urlsplit

from .config import Config

log = logging.getLogger("llm_summary.net")

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

_REQUESTS_ENV_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")


def _is_socks(url: str) -> bool:
    return url.startswith("socks5://") or url.startswith("socks5h://")


def requests_proxy_url(url: str) -> str:
    """Variant for requests: prefer proxy-side DNS (socks5h) for SOCKS proxies."""
    if url.startswith("socks5://"):
        return "socks5h://" + url[len("socks5://"):]
    return url


def httpx_proxy_url(url: str) -> str:
    """Variant for httpx: it only accepts the 'socks5' scheme (resolves remotely)."""
    if url.startswith("socks5h://"):
        return "socks5://" + url[len("socks5h://"):]
    return url


def apply_env_proxy(config: Config) -> None:
    """Export proxy env vars so requests/PyGithub route through the proxy.

    Always overwrites HTTP_PROXY/HTTPS_PROXY/ALL_PROXY with the normalized value
    so requests gets the socks5h scheme even if the user exported socks5.
    """
    url = config.proxy.url.strip()
    if not url:
        return
    req_url = requests_proxy_url(url)
    for key in _REQUESTS_ENV_KEYS:
        os.environ[key] = req_url
    log.info("Outbound proxy enabled (%s)", _redact(req_url))
    _warn_if_localhost_in_container(url)


def _in_container() -> bool:
    return os.path.exists("/.dockerenv")


def _warn_if_localhost_in_container(url: str) -> None:
    """A localhost proxy inside a container points at the container, not the host."""
    host = (urlsplit(url).hostname or "").lower()
    if host in _LOCAL_HOSTS and _in_container():
        log.warning(
            "Proxy host %r is loopback but we appear to be running in a container; "
            "this targets the container itself. Use host.docker.internal to reach a "
            "proxy on the host (the compose file maps it for Linux too).",
            host,
        )


def build_httpx_client(config: Config):
    """Build a sync httpx.Client bound to the proxy, or None if no proxy is set.

    Used for the OpenAI/LLM client. trust_env=False so the socks5h env exported
    for requests does not leak into httpx (which would reject that scheme).
    """
    url = config.proxy.url.strip()
    if not url:
        return None
    import httpx

    return httpx.Client(proxy=httpx_proxy_url(url), trust_env=False, timeout=60.0)


def build_httpx_async_client(config: Config):
    """Async counterpart of build_httpx_client.

    langchain-openai injects a keepalive transport that disables httpx's env-based
    proxy auto-detection, so we must pass an explicit async client too; otherwise
    async LLM calls would bypass the proxy.
    """
    url = config.proxy.url.strip()
    if not url:
        return None
    import httpx

    return httpx.AsyncClient(proxy=httpx_proxy_url(url), trust_env=False, timeout=60.0)


def _redact(url: str) -> str:
    """Hide any user:pass credentials embedded in a proxy URL before logging."""
    if "@" not in url:
        return url
    scheme, _, rest = url.partition("://")
    creds, _, host = rest.rpartition("@")
    if creds:
        return f"{scheme}://***@{host}"
    return url
