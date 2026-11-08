"""#232 R10: source-only, in-memory Gateway opt-in and no-8K-fallback proof.

No physical Worker, Native inference, external HTTP, disk/DB or GPU access.
All Serving hashes and Worker replies in this module are synthetic and fake.
"""

from __future__ import annotations

import asyncio
import json
import sys
from copy import deepcopy
from pathlib import Path

import httpx
import pytest

from astrumweaver import JobResult
from astrumweaver.control import InMemoryControlRepository, JobStatus
from astrumweaver.control.api import create_app
from astrumweaver.gateway.api import create_chat_router
from astrumweaver.gateway.chat import ChatProfileCatalog

# Match the source-only candidate tests: GitHub CI installs src/, not tools/.
# Make this test self-contained instead of depending on collection order.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from bonsai_12k_spica_candidate import LIMITS_8K, LIMITS_12K, PROFILE_8K, PROFILE_12K
from test_bonsai_12k_r9_managed_admission import (
    NEW_EPOCH,
    OLD_EPOCH,
    register,
    synthetic_only_chain,
)

BASE_URL = "http://in-memory-only"


def gateway(repo: InMemoryControlRepository, catalog):
    app = create_app(
        repo, client_token="synthetic-client-only",
        worker_token="synthetic-worker-only",
        maintenance_interval_seconds=60,
    )
    # Mirrors the observed current Control client_auth='none', not an
    # endorsement of no-auth for internet-facing reverse proxies.
    app.include_router(create_chat_router(
        repo, catalog, client_auth="none", client_token="synthetic-client-only",
        poll_interval_seconds=0.005,
    ))
    return app


def request_body(model=PROFILE_12K, *, max_tokens=64):
    return {
        "model": model, "messages": [{"role": "user", "content": "synthetic"}],
        "max_tokens": max_tokens,
    }


@pytest.mark.asyncio
async def test_models_advertise_distinct_opt_in_without_editing_original_rows():
    source, _, _, catalog, _ = synthetic_only_chain()
    old_catalog = deepcopy(source["control_catalog"])
    repo = InMemoryControlRepository()
    app = gateway(repo, catalog)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=BASE_URL,
    ) as client:
        models = await client.get("/v1/models")
    assert models.status_code == 200
    data = models.json()["data"]
    assert [row["id"] for row in data] == [
        "qwen3.8-27b", PROFILE_8K, PROFILE_12K,
    ]
    assert data[:2] == ChatProfileCatalog.from_dict(old_catalog).models_response()["data"]
    assert data[1]["x_astrumweaver"]["effective_limits"] == LIMITS_8K
    assert data[2]["x_astrumweaver"]["effective_limits"] == LIMITS_12K
    assert source["control_catalog"] == old_catalog
    assert repo.list_jobs() == []


@pytest.mark.asyncio
async def test_explicit_12k_http_fails_503_when_only_spica_deneb_8k_online():
    source, _, _, catalog, _ = synthetic_only_chain()
    from astrumweaver.serving import ServingDeploymentDeclaration

    old = ServingDeploymentDeclaration.from_dict(source["spica_serving"])
    repo = InMemoryControlRepository()
    register(repo, "bonsai-spica", old, OLD_EPOCH)
    register(repo, "bonsai-deneb", old, OLD_EPOCH)
    app = gateway(repo, catalog)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=BASE_URL,
    ) as client:
        answer = await asyncio.wait_for(
            client.post("/v1/chat/completions", json=request_body()),
            timeout=5,
        )
        missing = await client.post("/v1/chat/completions", json=request_body("bonsai2-27b-12k-does-not-exist"))
    assert answer.status_code == 503
    assert answer.json()["error"]["code"] == "no_compatible_deployment"
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "model_not_found"
    assert repo.list_jobs() == []
    assert repo.get_worker("bonsai-spica").active_jobs == 0
    assert repo.get_worker("bonsai-deneb").active_jobs == 0


@pytest.mark.asyncio
async def test_output_and_request_byte_limits_reject_before_synthetic_job():
    _, _, _, catalog, _ = synthetic_only_chain()
    repo = InMemoryControlRepository()
    app = gateway(repo, catalog)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=BASE_URL,
    ) as client:
        for streaming in (False, True):
            over_output = await client.post(
                "/v1/chat/completions",
                json={**request_body(max_tokens=2049), "stream": streaming},
            )
            assert over_output.status_code == 400
            assert over_output.json()["error"]["code"] == "output_limit_exceeded"

        # A valid JSON object but an HTTP body above the 65,536-byte cap.
        small = json.dumps(request_body()).encode("utf-8")
        exact_cap = small[:-1] + b" " * (65536 - len(small)) + b"}"
        assert len(exact_cap) == LIMITS_12K["request_bytes"]
        at_cap = await client.post(
            "/v1/chat/completions", content=exact_cap,
            headers={"content-type": "application/json"},
        )
        # Request passes the byte cap and reaches Control admission, where
        # the missing 12K Worker still fails closed. It is NOT native input.
        assert at_cap.status_code == 503
        assert at_cap.json()["error"]["code"] == "no_compatible_deployment"
        oversized = exact_cap[:-1] + b" " + b"}"
        assert len(oversized) == LIMITS_12K["request_bytes"] + 1
        too_large = await client.post(
            "/v1/chat/completions", content=oversized,
            headers={"content-type": "application/json"},
        )
        assert too_large.status_code == 413
        assert too_large.json()["error"]["code"] == "request_too_large"

    assert repo.list_jobs() == []


async def run_fake_only(repo, *, worker_id: str, epoch: str):
    """Complete exactly one in-memory job; never contacts any model."""
    loop = asyncio.get_running_loop()
    until = loop.time() + 3
    claim = None
    while claim is None and loop.time() < until:
        claim = repo.claim_next_job(worker_id, runtime_instance_epoch=epoch)
        if claim is None:
            await asyncio.sleep(0.005)
    assert claim is not None, "synthetic job was never claimed"
    repo.complete_job(
        claim.job_id,
        JobResult(outputs={
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": f"FAKE:{worker_id}"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
        }),
        worker_id=worker_id, lease_token=claim.lease_token or "",
        runtime_instance_epoch=epoch,
    )
    return claim


@pytest.mark.asyncio
async def test_fake_12k_worker_accepts_only_opt_in_while_deneb_keeps_8k():
    from astrumweaver.serving import ServingDeploymentDeclaration

    source, _, new, catalog, _ = synthetic_only_chain()
    old = ServingDeploymentDeclaration.from_dict(source["deneb_serving"])
    repo = InMemoryControlRepository()
    register(repo, "bonsai-deneb", old, OLD_EPOCH)
    register(repo, "synthetic-spica-12k", new, NEW_EPOCH)
    app = gateway(repo, catalog)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=BASE_URL,
    ) as client:
        first = asyncio.create_task(run_fake_only(
            repo, worker_id="synthetic-spica-12k", epoch=NEW_EPOCH,
        ))
        try:
            response12 = await asyncio.wait_for(
                client.post("/v1/chat/completions", json=request_body()), timeout=5,
            )
            claim12 = await asyncio.wait_for(first, timeout=3)
        finally:
            if not first.done():
                first.cancel()
                await asyncio.gather(first, return_exceptions=True)
        assert response12.status_code == 200
        assert response12.json()["choices"][0]["message"]["content"] == "FAKE:synthetic-spica-12k"
        assert claim12.claimed_deployment_revision == new.deployment.revision
        assert claim12.claimed_serving_contract_revision == new.contracts[0].revision
        assert claim12.claimed_runtime_instance_epoch == NEW_EPOCH

        second = asyncio.create_task(run_fake_only(
            repo, worker_id="bonsai-deneb", epoch=OLD_EPOCH,
        ))
        try:
            response8 = await asyncio.wait_for(
                client.post("/v1/chat/completions", json=request_body(PROFILE_8K)),
                timeout=5,
            )
            claim8 = await asyncio.wait_for(second, timeout=3)
        finally:
            if not second.done():
                second.cancel()
                await asyncio.gather(second, return_exceptions=True)
        assert response8.status_code == 200
        assert response8.json()["choices"][0]["message"]["content"] == "FAKE:bonsai-deneb"
        assert claim8.claimed_deployment_revision == old.deployment.revision
        assert claim8.claimed_serving_contract_revision == old.contracts[0].revision
        assert claim8.claimed_runtime_instance_epoch == OLD_EPOCH

    rows = repo.list_jobs()
    assert len(rows) == 2
    assert all(job.status is JobStatus.SUCCEEDED for job in rows)
    assert {row.serving for row in rows} == {
        catalog.get(PROFILE_8K).binding, catalog.get(PROFILE_12K).binding,
    }
