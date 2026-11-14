import os

from websearch.cli import _load_env_file


def test_env_file_fills_missing_and_explicit_wins(tmp_path, monkeypatch):
    name = "AGENT_WEBSEARCH_TEST_ENV_FILE_VALUE"
    monkeypatch.delenv(name, raising=False)
    path = tmp_path / "env"
    path.write_text(
        f"# comment\n{name}=\"from-file\"\nMALFORMED LINE\nEMPTY=\n",
        encoding="utf-8",
    )

    _load_env_file(path)
    assert os.environ[name] == "from-file"
    assert os.environ["EMPTY"] == ""

    monkeypatch.setenv(name, "explicit")
    _load_env_file(path)
    assert os.environ[name] == "explicit"
