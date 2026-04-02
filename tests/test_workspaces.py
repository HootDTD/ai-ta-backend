from pathlib import Path

from workspaces import build_local_static_workspace_config, build_workspace_manager


def _make_index_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "faiss.index").write_bytes(b"")
    (path / "sqlite.db").write_bytes(b"")
    (path / "items.jsonl").write_text("", encoding="utf-8")
    return path


def test_build_local_static_workspace_config_from_env_doc_sets(monkeypatch, tmp_path):
    index_dir = _make_index_dir(tmp_path / "local_index")

    monkeypatch.setenv("LEGACY_CLASS_NAME", "Local Test Class")
    monkeypatch.setenv("LEGACY_CLASS_SUBJECT", "Local Subject")
    monkeypatch.setenv("LEGACY_CLASS_DOC_SETS", str(index_dir))

    cfg = build_local_static_workspace_config()

    assert "Local Test Class" in cfg
    record = cfg["Local Test Class"]
    assert record["subject"] == "Local Subject"
    assert record["doc_sets"] == [str(index_dir.resolve())]


def test_build_workspace_manager_uses_local_static_fallback(monkeypatch, tmp_path):
    index_dir = _make_index_dir(tmp_path / "fallback_index")
    class_name = "AAE 33300: Introduction to Fluid Mechanics"

    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_API_KEY", raising=False)
    monkeypatch.delenv("USE_PGVECTOR_RETRIEVAL", raising=False)
    monkeypatch.setenv("LEGACY_CLASS_NAME", class_name)
    monkeypatch.setenv("LEGACY_CLASS_SUBJECT", "Fluid Mechanics")
    monkeypatch.setenv("LEGACY_CLASS_DOC_SETS", str(index_dir))

    manager = build_workspace_manager()
    workspace = manager.get(class_name)

    assert workspace.class_name == class_name
    assert workspace.subject_name == "Fluid Mechanics"
    assert workspace.doc_sets() == [index_dir.resolve()]
