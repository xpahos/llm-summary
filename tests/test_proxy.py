from llm_summary import net
from llm_summary.config import Config, ProxyConfig, load_config

PROXY_ENV_KEYS = [
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "all_proxy",
    "LLM_SUMMARY_PROXY",
]


def _clear_proxy_env(monkeypatch):
    for key in PROXY_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_scheme_normalization():
    # requests wants socks5h (remote DNS); httpx wants socks5.
    assert net.requests_proxy_url("socks5://h:1080") == "socks5h://h:1080"
    assert net.requests_proxy_url("socks5h://h:1080") == "socks5h://h:1080"
    assert net.httpx_proxy_url("socks5h://h:1080") == "socks5://h:1080"
    assert net.httpx_proxy_url("socks5://h:1080") == "socks5://h:1080"
    # http(s) proxies pass through unchanged for both.
    assert net.requests_proxy_url("http://h:3128") == "http://h:3128"
    assert net.httpx_proxy_url("http://h:3128") == "http://h:3128"


def test_apply_env_proxy_sets_socks5h(monkeypatch):
    _clear_proxy_env(monkeypatch)
    cfg = Config(proxy=ProxyConfig(url="socks5://proxy.corp:1080"))
    net.apply_env_proxy(cfg)
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        assert net.os.environ[key] == "socks5h://proxy.corp:1080"


def test_apply_env_proxy_noop_when_empty(monkeypatch):
    _clear_proxy_env(monkeypatch)
    net.apply_env_proxy(Config())
    assert "ALL_PROXY" not in net.os.environ


def test_build_httpx_client(monkeypatch):
    assert net.build_httpx_client(Config()) is None
    client = net.build_httpx_client(Config(proxy=ProxyConfig(url="socks5h://h:1080")))
    assert client is not None
    client.close()


def test_redaction():
    assert net._redact("socks5h://user:pass@h:1080") == "socks5h://***@h:1080"
    assert net._redact("socks5h://h:1080") == "socks5h://h:1080"


def test_localhost_in_container_warns(monkeypatch, caplog):
    monkeypatch.setattr(net, "_in_container", lambda: True)
    with caplog.at_level("WARNING"):
        net._warn_if_localhost_in_container("socks5h://localhost:1080")
    assert any("host.docker.internal" in r.message for r in caplog.records)


def test_remote_host_in_container_no_warn(monkeypatch, caplog):
    monkeypatch.setattr(net, "_in_container", lambda: True)
    with caplog.at_level("WARNING"):
        net._warn_if_localhost_in_container("socks5h://host.docker.internal:1080")
    assert not caplog.records


def test_localhost_outside_container_no_warn(monkeypatch, caplog):
    monkeypatch.setattr(net, "_in_container", lambda: False)
    with caplog.at_level("WARNING"):
        net._warn_if_localhost_in_container("socks5h://localhost:1080")
    assert not caplog.records


def test_config_picks_up_proxy_env(monkeypatch):
    _clear_proxy_env(monkeypatch)
    monkeypatch.setenv("LLM_SUMMARY_PROXY", "socks5h://explicit:1080")
    monkeypatch.setenv("ALL_PROXY", "socks5h://fallback:1080")
    cfg = load_config(None)
    assert cfg.proxy.url == "socks5h://explicit:1080"  # explicit wins


def test_config_falls_back_to_all_proxy(monkeypatch):
    _clear_proxy_env(monkeypatch)
    monkeypatch.setenv("ALL_PROXY", "socks5h://fallback:1080")
    cfg = load_config(None)
    assert cfg.proxy.url == "socks5h://fallback:1080"
