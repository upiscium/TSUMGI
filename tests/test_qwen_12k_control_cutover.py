"""#230 R14: Control profile is loaded on startup, not hot-reloaded."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from astrumweaver.control import InMemoryControlRepository
from astrumweaver.gateway.api import ChatGatewayService, load_chat_catalog

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))

import qwen_12k_candidate as qwen
from test_qwen_12k_candidate import (
    fixtures,
    make_draft,
    synthetic_revision_chain,
)


def test_control_process_catalog_is_immutable_until_new_load(tmp_path: Path):
    runtime, serving, catalog_8k = fixtures()
    draft = make_draft(runtime, serving, catalog_8k)
    _, _, catalog_12k = synthetic_revision_chain(draft)
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(catalog_8k), encoding="utf-8")
    loaded_8k = load_chat_catalog(str(path))
    service = ChatGatewayService(InMemoryControlRepository(), loaded_8k)
    assert service.catalog.get(qwen.PROFILE_ID).resolved.effective_limits["total_tokens"] == 8192
    # Writing the file alone cannot mutate an existing in-process catalog.
    replacement = tmp_path / "replacement.json"
    replacement.write_text(json.dumps(catalog_12k), encoding="utf-8")
    replacement.replace(path)
    assert service.catalog.get(qwen.PROFILE_ID).resolved.effective_limits["total_tokens"] == 8192
    # A fresh explicit catalog load is needed (production daemon loads at startup).
    reloaded = load_chat_catalog(str(path))
    assert reloaded.get(qwen.PROFILE_ID).resolved.effective_limits["total_tokens"] == 12288
    assert service.catalog.get(qwen.PROFILE_ID).resolved.effective_limits["total_tokens"] == 8192


def test_unreviewed_invalid_control_catalog_cannot_be_loaded(tmp_path: Path):
    runtime, serving, catalog_8k = fixtures()
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(catalog_8k), encoding="utf-8")
    original = load_chat_catalog(str(path))
    draft = make_draft(runtime, serving, catalog_8k)
    path.write_text(json.dumps(draft["control_UNLOADABLE_draft"]), encoding="utf-8")
    with pytest.raises(RuntimeError, match="catalog"):
        load_chat_catalog(str(path))
    assert original.get(qwen.PROFILE_ID).resolved.effective_limits["total_tokens"] == 8192
