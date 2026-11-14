import stat

from websearch.config import Config


def test_env_overrides_file_and_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("WEBSEARCH_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text("[server]\nport = 9000\n", encoding="utf-8")
    monkeypatch.setenv("WEBSEARCH_SERVER__PORT", "8000")
    monkeypatch.setenv("WEBSEARCH_SEARCH__CACHE_TTL", "0")

    config = Config.load()

    assert config.server.port == 8000
    assert config.search.cache_ttl == 0.0
    assert config.search.default_count == 10


def test_relative_paths_resolve_against_home(tmp_path, monkeypatch):
    monkeypatch.delenv("WEBSEARCH_HOME", raising=False)
    config = Config(home=tmp_path)
    assert config.learned_path == tmp_path / "blocked-domains.txt"
    assert config.pool_path == tmp_path / "pool.txt"
    assert config.token_path == tmp_path / "token"


def test_token_is_created_once_with_0600(tmp_path, monkeypatch):
    monkeypatch.delenv("WEBSEARCH_TOKEN", raising=False)
    config = Config(home=tmp_path)

    first = config.ensure_token()
    second = config.ensure_token()

    assert first == second
    path = tmp_path / "token"
    assert path.read_text().strip() == first
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
