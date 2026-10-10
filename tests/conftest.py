import pytest


@pytest.fixture(autouse=True)
def _isolated_dirs(tmp_path, monkeypatch):
    """Keep tests out of the real runs/ and cache/ directories."""
    monkeypatch.setenv("NAVEGADOR_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("NAVEGADOR_CACHE_DIR", str(tmp_path / "cache"))
