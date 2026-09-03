import pytest

from mas.tools import build_search_tool


def test_none_backend_returns_no_sources():
    assert build_search_tool("none")("anything") == []


def test_unknown_backend_is_rejected_at_build_time():
    with pytest.raises(ValueError, match="unknown search backend"):
        build_search_tool("bing")


def test_search_failure_degrades_to_empty_instead_of_crashing_the_run(monkeypatch):
    import mas.tools.search as search_mod

    class Boom:
        def __enter__(self): raise RuntimeError("network down")
        def __exit__(self, *a): return False

    monkeypatch.setitem(__import__("sys").modules, "ddgs", type("m", (), {"DDGS": Boom}))
    assert search_mod._duckduckgo_search("query") == []
