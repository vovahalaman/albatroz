import pytest


@pytest.fixture(autouse=True)
def _isolated_dirs(tmp_path, monkeypatch):
    """Keep tests out of the real runs/ and cache/ directories."""
    monkeypatch.setenv("NAVEGADOR_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("NAVEGADOR_CACHE_DIR", str(tmp_path / "cache"))


@pytest.fixture(autouse=True)
def _no_real_llm_calls(monkeypatch):
    """pytest must never reach the Gemini API: any un-mocked request fails the test."""
    async def _blocked(*args, **kwargs):
        raise AssertionError("real LLM call attempted in tests; mock llm.call_sync / llm.call")

    monkeypatch.setattr("navegador.llm._generate", _blocked)
