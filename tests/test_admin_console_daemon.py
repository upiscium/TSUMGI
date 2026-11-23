from __future__ import annotations

import json

import httpx
import pytest

from tsumgi.control.daemon import _catalog_profile_names, build_app
from tsumgi.gateway.api import load_chat_catalog

from test_chat_gateway_daemon import base_environment, catalog_document


def write_config(tmp_path, *, admin: str | None, chat: bool = False) -> str:
    lines = ["[control]", 'client_auth = "none"', ""]
    if chat:
        catalog = tmp_path / "chat-catalog.json"
        catalog.write_text(json.dumps(catalog_document()), encoding="utf-8")
        lines += ["[chat_gateway]", "enabled = true", f'catalog = "{catalog}"', ""]
    if admin is not None:
        lines += ["[admin_ui]", f"enabled = {admin}", ""]
    path = tmp_path / "control.toml"
    path.write_text("\n".join(lines), encoding="utf-8")
    return str(path)


async def get(app, path):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://control") as client:
        return await client.get(path)


@pytest.mark.asyncio
@pytest.mark.parametrize("admin", [None, "false"])
async def test_admin_console_is_off_unless_enabled(tmp_path, monkeypatch, admin):
    base_environment(monkeypatch)
    app = build_app(write_config(tmp_path, admin=admin))
    assert not any(path.startswith("/admin") for path in app.openapi()["paths"])
    for path in ("/admin", "/admin/app.js", "/admin/api/workers"):
        assert (await get(app, path)).status_code == 404, path


@pytest.mark.asyncio
async def test_admin_console_mounts_when_enabled(tmp_path, monkeypatch):
    base_environment(monkeypatch)
    app = build_app(write_config(tmp_path, admin="true"))
    assert {"/admin/api/workers", "/admin/api/model-check"} <= set(app.openapi()["paths"])
    for path, kind in (("/admin", "text/html"), ("/admin/app.js", "text/javascript"), ("/admin/app.css", "text/css")):
        response = await get(app, path)
        assert response.status_code == 200 and response.headers["content-type"].startswith(kind), path


@pytest.mark.asyncio
async def test_allowed_hosts_from_the_config_are_enforced(tmp_path, monkeypatch):
    base_environment(monkeypatch)
    path = write_config(tmp_path, admin="true")
    with open(path, "a", encoding="utf-8") as handle:
        handle.write('allowed_hosts = ["control.lan"]\n')
    app = build_app(path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://x") as client:
        assert (await client.get("/admin", headers={"host": "control.lan:9000"})).status_code == 200
        assert (await client.get("/admin", headers={"host": "evil.example"})).status_code == 403


@pytest.mark.parametrize("value", ['"control.lan"', "[1, 2]", "true"])
def test_allowed_hosts_must_be_a_list_of_strings(tmp_path, monkeypatch, value):
    base_environment(monkeypatch)
    path = write_config(tmp_path, admin="true")
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"allowed_hosts = {value}\n")
    with pytest.raises(RuntimeError, match="allowed_hosts"):
        build_app(path)


@pytest.mark.parametrize("value", ['"yes"', "1"])
def test_admin_ui_enabled_must_be_boolean(tmp_path, monkeypatch, value):
    base_environment(monkeypatch)
    with pytest.raises(RuntimeError, match="admin_ui.enabled must be boolean"):
        build_app(write_config(tmp_path, admin=value))


def test_profile_names_map_deployment_revisions_to_profile_ids(tmp_path):
    catalog_path = tmp_path / "c.json"
    catalog_path.write_text(json.dumps(catalog_document()), encoding="utf-8")
    catalog = load_chat_catalog(str(catalog_path))
    expected = {p.resolved.deployment.revision: "local-code-v1" for p in catalog.profiles}
    assert _catalog_profile_names(catalog) == expected and expected
    assert _catalog_profile_names(object()) == {}
