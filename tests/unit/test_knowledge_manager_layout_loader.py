"""Unit tests for knowledge.manager._load_layout_module path resolution.

Regression guard: the layout embedder lives at <repo-root>/text-embeder/, not
knowledge/text-embeder/. The loader previously resolved one directory too
shallow and failed to import the embedder for every knowledge upload.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import knowledge.manager as manager_mod

REPO_ROOT = Path(manager_mod.__file__).resolve().parent.parent


@pytest.fixture
def fresh_layout_loader(monkeypatch):
    """Reset the module-level cache so the loader actually runs."""
    monkeypatch.setattr(manager_mod, "_LAYOUT_MODULE", None)
    yield
    sys.modules.pop("backend_layout_embedder", None)


@pytest.mark.unit
def test_load_layout_module_resolves_repo_root_script(monkeypatch, fresh_layout_loader):
    captured = {}
    sentinel_module = SimpleNamespace()

    class _FakeLoader:
        def exec_module(self, module):
            captured["executed"] = module

    def fake_spec_from_file_location(name, path):
        captured["path"] = Path(path)
        return SimpleNamespace(name=name, loader=_FakeLoader())

    monkeypatch.setattr(manager_mod, "spec_from_file_location", fake_spec_from_file_location)
    monkeypatch.setattr(manager_mod, "module_from_spec", lambda spec: sentinel_module)

    module = manager_mod._load_layout_module()

    expected = REPO_ROOT / "text-embeder" / "layout_multimodal_embedder.py"
    assert captured["path"] == expected
    assert expected.is_file(), "embedder script must exist at the resolved path"
    assert captured["executed"] is sentinel_module
    assert module is sentinel_module


@pytest.mark.unit
def test_load_layout_module_caches_loaded_module(monkeypatch, fresh_layout_loader):
    sentinel_module = SimpleNamespace()

    def fail_if_called(*args, **kwargs):  # pragma: no cover - guard only
        raise AssertionError("loader must not re-import once cached")

    monkeypatch.setattr(manager_mod, "_LAYOUT_MODULE", sentinel_module)
    monkeypatch.setattr(manager_mod, "spec_from_file_location", fail_if_called)

    assert manager_mod._load_layout_module() is sentinel_module
