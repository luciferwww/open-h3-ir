"""Asset cards preserve original-language text regardless of the host's default encoding."""
from dataclasses import asdict, replace
import json
from pathlib import Path

import pytest

from h3ir import analyse
from h3ir.config import get_config
from h3ir.models import AssetCard, AssetKind, AssetRef, Role


@pytest.fixture
def cache(tmp_path, monkeypatch):
    cfg = get_config()
    monkeypatch.setattr(analyse, "get_config",
                        lambda: replace(cfg, paths=replace(cfg.paths, state_dir=tmp_path)))
    ref = AssetRef(kind=AssetKind.IMAGE, role=Role.SUBJECT, sha256="e" * 64)
    card = AssetCard(sha256=ref.sha256, kind=ref.kind,
                     summary="她走出火车站。", visible_text=["出口", "Café", "営業中"])
    return ref, card, analyse._cache_path(analyse._cache_key(ref, "encoding-test"))


def windows_default(monkeypatch):
    original = Path.open

    def open_cp1252(self, mode="r", buffering=-1, encoding=None, errors=None, newline=None):
        if "b" not in mode and encoding is None:
            encoding = "cp1252"
        return original(self, mode=mode, buffering=buffering, encoding=encoding,
                        errors=errors, newline=newline)

    monkeypatch.setattr(Path, "open", open_cp1252)


def test_cache_write_is_utf8_under_windows_default(cache, monkeypatch):
    ref, card, path = cache
    windows_default(monkeypatch)
    analyse.save_cached(ref, card, "encoding-test")
    raw = json.loads(path.read_bytes().decode("utf-8"))
    assert raw["summary"] == card.summary
    assert raw["visible_text"] == card.visible_text


def test_cache_read_is_utf8_under_windows_default(cache, monkeypatch):
    ref, card, path = cache
    raw = asdict(card)
    raw["kind"] = card.kind.value
    path.write_bytes(json.dumps(raw, ensure_ascii=False).encode("utf-8"))
    windows_default(monkeypatch)
    restored = analyse.load_cached(ref, "encoding-test")
    assert restored == card
