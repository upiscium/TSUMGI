"""Private UDS native transport prevents cross-UID TCP port impersonation (#123)."""
from __future__ import annotations

import asyncio
import stat
from pathlib import Path

import httpx
import pytest

from astrumweaver.runtime import RuntimeDeploymentSpec, managed_runtime_from_deployment
from astrumweaver.runtime.providers.llama_cpp import HttpLlamaCppApi
from test_dual_llama_cpp import setup_pair, vars_config


@pytest.mark.asyncio
async def test_dual_children_use_0700_owned_uds_not_manifest_tcp_ports(tmp_path):
    provider, ctx = setup_pair(tmp_path)
    deployment = RuntimeDeploymentSpec(
        provider_id=provider.info.provider_id,
        provider_config=vars_config(provider.config),
        demand=ctx.demand, setup_intent=provider.setup_intent(ctx),
    )
    runtime = managed_runtime_from_deployment(
        deployment, worker=ctx.worker, host=ctx.host,
    )
    auth_root = Path(runtime._private_auth_files.name)
    paths = []
    try:
        assert stat.S_IMODE(auth_root.stat().st_mode) == 0o700
        for role,child in (("embedding",runtime._embedding),("decision",runtime._decision)):
            path = Path(child.process.unix_socket_path)
            assert path == auth_root / (role + ".sock")
            assert path.is_absolute()
            assert path.parent == auth_root
            args = child.process.command()
            assert args[args.index("--host") + 1] == str(path)
            assert "--port" not in args, "no fixed TCP listener for an attacker to race"
            assert "--api-key-file" in args
            assert "Authorization" not in " ".join(args)
            assert child.api._client.headers["authorization"].startswith("Bearer ")
            paths.append(path)
        assert paths[0] != paths[1]
    finally:
        await runtime.release()
    assert not auth_root.exists()


@pytest.mark.asyncio
async def test_foreign_fixed_loopback_listener_never_receives_private_token(tmp_path):
    """Even a pre-existing attacker listener must not see /health or the bearer."""
    seen = []
    async def attacker(reader, writer):
        seen.append(await reader.read(4096))
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(attacker, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    private_socket = str(tmp_path/"native.sock")
    api = HttpLlamaCppApi(
        f"http://127.0.0.1:{port}",
        api_key="private-do-not-send",
        unix_socket_path=private_socket,
        timeout_seconds=0.5,
    )
    try:
        assert await api.health() is False  # No owned UDS socket exists yet
        assert seen == []
    finally:
        await api.close()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_uds_transport_never_inherits_environment_proxy(tmp_path,monkeypatch):
    observed=[]
    constructor=httpx.AsyncClient
    transport_factory=httpx.AsyncHTTPTransport
    def capture_client(*args,**kwargs):
        observed.append(("client",dict(kwargs)))
        return constructor(*args,**kwargs)
    def capture_transport(*args,**kwargs):
        observed.append(("transport",dict(kwargs)))
        return transport_factory(*args,**kwargs)
    monkeypatch.setattr(httpx,"AsyncClient",capture_client)
    monkeypatch.setattr(httpx,"AsyncHTTPTransport",capture_transport)
    monkeypatch.setenv("HTTP_PROXY","http://attacker.invalid:1234")
    monkeypatch.setenv("NO_PROXY","")
    socket_path=str(tmp_path/"owned.sock")
    api=HttpLlamaCppApi("http://127.0.0.1:19999",
        api_key="not-for-proxy",unix_socket_path=socket_path)
    try:
        args=dict(observed)
        assert args["transport"]["uds"]==socket_path
        assert args["client"]["trust_env"] is False
        assert args["client"]["follow_redirects"] is False
        assert args["client"]["headers"]["Authorization"]=="Bearer not-for-proxy"
    finally:
        await api.close()


def test_uds_requires_bearer_and_absolute_path(tmp_path):
    with pytest.raises(ValueError,match="requires"):
        HttpLlamaCppApi("http://127.0.0.1:18700",unix_socket_path=str(tmp_path/"a.sock"))
    with pytest.raises(ValueError,match="requires"):
        HttpLlamaCppApi("http://127.0.0.1:18700",api_key="k",unix_socket_path="relative.sock")
    with pytest.raises(ValueError,match="requires"):
        HttpLlamaCppApi("http://127.0.0.1:18700",api_key="k",unix_socket_path=str(tmp_path/"not-sock"),transport=httpx.MockTransport(lambda req:httpx.Response(200)))
