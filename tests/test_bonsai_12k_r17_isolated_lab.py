"""#232 R17 offline/ASGI-only Control isolation. No Spica, GPU or real Job."""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from bonsai_12k_r17_lab_config import (
    GPU,
    LAB_NATIVE_URL,
    LAB_PROFILE,
    LAB_WORKER,
    LIMITS_12K,
    render_isolated_lab,
)
from bonsai_12k_r17_lab_control import create_lab_app
from test_bonsai_12k_spica_candidate import make_inputs as abbreviated_make_inputs

from astrumweaver.gateway.chat import ChatProfileCatalog
from astrumweaver.runtime.deployment import RuntimeDeploymentSpec
from astrumweaver.serving import ServingDeploymentDeclaration

CLIENT = "r17-client-token-isolated-synthetic-only"
WORKER = "r17-worker-token-isolated-synthetic-only"


def make_inputs():
    """Expand the R0 abbreviated synthetic setup to live Spica field parity."""
    x = abbreviated_make_inputs()
    provider = x["spica_runtime"]["provider_config"]
    setup = x["spica_runtime"]["setup_intent"]["configuration"]
    exceptions = {"package_reference", "startup_timeout_seconds",
                  "request_timeout_seconds"}
    for key in set(provider) - exceptions:
        setup.setdefault(key, "none" if key == "split_mode"
                         and provider[key] is None else provider[key])
    return x


def build():
    base = make_inputs()
    before = copy.deepcopy(base)
    candidate = render_isolated_lab(
        original_8k_runtime=base["spica_runtime"],
        original_8k_serving=base["spica_serving"],
    )
    assert before == base, "old 8K fixture mutated"
    return candidate, base


def test_lab_profile_is_distinct_from_production_and_exact_12k():
    data, _ = build()
    assert data["admission"] == "LAB_ONLY_UNATTESTED_SERVING_IDENTITY"
    assert data["new_lab_profile_id"] == LAB_PROFILE
    assert data["new_lab_worker_id"] == LAB_WORKER
    assert data["production_serving_identity_issued"] is False
    assert data["production_control_catalog_modified"] is False
    assert data["deployment_authorized"] is False
    runtime = data["new_lab_runtime"]
    serving = data["new_lab_serving"]
    profile = ChatProfileCatalog.from_dict(data["new_lab_catalog"])
    decl = ServingDeploymentDeclaration.from_dict(serving)
    assert RuntimeDeploymentSpec.from_dict(runtime).provider_id == "llama-cpp"
    assert runtime["provider_config"]["context_size"] == 12288
    assert runtime["setup_intent"]["configuration"]["context_size"] == 12288
    assert runtime["setup_intent"]["configuration"]["gpu_uuids"] == [GPU]
    assert runtime["provider_config"]["base_url"] == LAB_NATIVE_URL
    assert runtime["setup_intent"]["configuration"]["base_url"] == LAB_NATIVE_URL
    assert profile.profiles[0].resolved.deployment.revision == decl.deployment.revision
    assert profile.profiles[0].resolved.contract.revision == decl.contracts[0].revision
    assert profile.profiles[0].resolved.effective_limits == LIMITS_12K
    assert [m["id"] for m in profile.models_response()["data"]] == [LAB_PROFILE]


def test_lab_sha_claims_are_distinct_but_not_attested():
    data, base = build()
    old = base["spica_serving"]
    deployment = data["new_lab_serving"]["deployment"]
    assert deployment["execution_config_sha256"].startswith("sha256:")
    assert len(deployment["execution_config_sha256"]) == 71
    assert data["lab_execution_config_claim_sha256"] != old["deployment"]["execution_config_sha256"]
    assert data["lab_validation_evidence_claim_sha256"] != old["contracts"][0]["contract"]["validation_evidence_sha256"]
    assert data["lab_execution_config_claim_sha256"] != data["lab_validation_evidence_claim_sha256"]
    assert data["new_lab_serving"]["contracts"][0]["contract"]["limits"] == LIMITS_12K


@pytest.mark.parametrize(("where", "field", "bad"), [
    ("runtime", "context_size", 12288),
    ("runtime", "offline", False),
    ("runtime", "jinja", False),
    ("runtime", "runtime_flavor", "upstream"),
    ("runtime", "fit", True),
])
def test_lab_cannot_be_built_from_unqualified_source(where, field, bad):
    original = make_inputs()
    original["spica_runtime"]["provider_config"][field] = bad
    with pytest.raises((ValueError, RuntimeError)):
        render_isolated_lab(
            original_8k_runtime=original["spica_runtime"],
            original_8k_serving=original["spica_serving"],
        )


@pytest.mark.asyncio
async def test_actual_fastapi_loopback_lab_models_auth_and_no_8k_fallback():
    data, _ = build()
    app = create_lab_app(
        data["new_lab_catalog"], client_token=CLIENT, worker_token=WORKER,
    )
    assert app.state.lab_repository.list_jobs() == []
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://r17-in-memory-only",
    ) as client:
        unauth = await client.get("/v1/models")
        assert unauth.status_code == 401
        unauthorized_status = await client.get("/__lab/status")
        assert unauthorized_status.status_code == 401
        authorization = {"authorization": f"Bearer {CLIENT}"}
        models = await client.get("/v1/models", headers=authorization)
        assert models.status_code == 200
        assert [m["id"] for m in models.json()["data"]] == [LAB_PROFILE]
        status = await client.get("/__lab/status", headers=authorization)
        assert status.status_code == 200
        assert status.json()["workers"] == []
        assert status.json()["jobs"] == []
        assert status.json()["production_eligible"] is False
        wrong = await client.post("/v1/chat/completions", headers=authorization, json={
            "model": "bonsai2-27b", "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 8,
        })
        assert wrong.status_code == 404
        assert wrong.json()["error"]["code"] == "model_not_found"
        missing_worker = await client.post("/v1/chat/completions", headers=authorization, json={
            "model": LAB_PROFILE, "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 8,
        })
        assert missing_worker.status_code == 503
        assert missing_worker.json()["error"]["code"] == "no_compatible_deployment"
        overlimit = await client.post("/v1/chat/completions", headers=authorization, json={
            "model": LAB_PROFILE, "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 2049,
        })
        assert overlimit.status_code == 400
        assert overlimit.json()["error"]["code"] == "output_limit_exceeded"
    assert app.state.lab_repository.list_jobs() == []


def test_lab_rejects_weak_auth_and_extra_production_catalog_rows():
    data, _ = build()
    for c,w in (("short",WORKER),(CLIENT,CLIENT)):
        with pytest.raises(ValueError, match="lab-bearer-secrets-invalid"):
            create_lab_app(data["new_lab_catalog"],client_token=c,worker_token=w)
    catalog = copy.deepcopy(data["new_lab_catalog"])
    catalog["profiles"].append(copy.deepcopy(catalog["profiles"][0]))
    catalog["profiles"][1]["profile"]["profile_id"] = "bonsai2-27b"
    with pytest.raises(ValueError):
        create_lab_app(catalog,client_token=CLIENT,worker_token=WORKER)


def test_lab_repository_rejects_foreign_worker_and_unqualified_serving():
    from astrumweaver import ResourceShape, WorkerSpec
    from astrumweaver.control import WorkerRegistration
    from astrumweaver.serving import RuntimeInstance, WorkerServingAdvertisement

    data, _ = build()
    app = create_lab_app(data["new_lab_catalog"], client_token=CLIENT, worker_token=WORKER)
    repo = app.state.lab_repository
    declaration = ServingDeploymentDeclaration.from_dict(data["new_lab_serving"])
    advertisement = WorkerServingAdvertisement(
        deployment_revision=declaration.deployment.revision,
        runtime_instance=RuntimeInstance(
            declaration.deployment.revision,
            "33333333-3333-4333-8333-333333333333",
        ),
        contracts=declaration.contracts,
    )

    def register(worker_id, serving):
        return repo.register_worker(WorkerRegistration(
            spec=WorkerSpec(
                worker_id=worker_id,
                worker_class="gpu-single",
                resources=ResourceShape(),
                capabilities=frozenset({"llm.chat", "text.generate"}),
            ),
            serving=serving,
        ))

    with pytest.raises(ValueError, match="foreign-or-unqualified-worker"):
        register("bonsai-spica", advertisement)
    with pytest.raises(ValueError, match="foreign-or-unqualified-worker"):
        register(LAB_WORKER, None)
    assert repo.list_jobs() == []
    valid = register(LAB_WORKER, advertisement)
    assert valid.spec.worker_id == LAB_WORKER
    assert valid.serving.runtime_instance.epoch == "33333333-3333-4333-8333-333333333333"


@pytest.mark.parametrize(("field", "bad"), [
    ("offline", False), ("no_webui", False), ("jinja", False),
    ("fit", True), ("gpu_layers", 0),
    ("context_size", 4096),
])
def test_lab_setup_intent_source_drift_is_rejected(field, bad):
    original = make_inputs()
    original["spica_runtime"]["setup_intent"]["configuration"][field] = bad
    with pytest.raises(ValueError, match="source-provider-setup-diverged|unexpected-bonsai-8k-runtime-baseline"):
        render_isolated_lab(
            original_8k_runtime=original["spica_runtime"],
            original_8k_serving=original["spica_serving"],
        )


def test_lab_unreviewed_original_gpu_and_model_ref_rejected():
    original = make_inputs()
    original["spica_runtime"]["setup_intent"]["configuration"]["gpu_uuids"] = [
        "GPU-12345678-1234-1234-1234-123456789abc"
    ]
    with pytest.raises(ValueError, match="unexpected-source-setup-intent"):
        render_isolated_lab(
            original_8k_runtime=original["spica_runtime"],
            original_8k_serving=original["spica_serving"],
        )
    original = make_inputs()
    original["spica_runtime"]["setup_intent"]["configuration"]["model_ref"] = (
        "/unexpected/location/model.gguf"
    )
    with pytest.raises(ValueError, match="unexpected-source-setup-intent"):
        render_isolated_lab(
            original_8k_runtime=original["spica_runtime"],
            original_8k_serving=original["spica_serving"],
        )


@pytest.mark.asyncio
async def test_live_control_worker_registration_api_passes_recovery_keyword():
    """Regression: actual Control API always supplies recovery= to repo."""
    from astrumweaver.serving import RuntimeInstance, WorkerServingAdvertisement

    data, _ = build()
    app = create_lab_app(data["new_lab_catalog"], client_token=CLIENT, worker_token=WORKER)
    declaration = ServingDeploymentDeclaration.from_dict(data["new_lab_serving"])
    serving = WorkerServingAdvertisement(
        deployment_revision=declaration.deployment.revision,
        runtime_instance=RuntimeInstance(
            declaration.deployment.revision,
            "55555555-5555-4555-8555-555555555555",
        ),
        contracts=declaration.contracts,
    )
    body = {
        "protocol_version": "v1",
        "recovery": False,
        "spec": {
            "worker_id": LAB_WORKER,
            "worker_class": "gpu-single",
            "gpu_uuids": [],
            "accelerators": [],
            "capabilities": ["llm.chat", "text.generate"],
            "labels": {},
            "resources": {
                "gpu_count": 0,
                "total_vram_mb": 0,
                "max_single_gpu_vram_mb": 0,
            },
        },
        "max_concurrency": 1,
        "metadata": {},
        "serving": serving.to_dict(),
        "extensions": ["serving-bindings-v1"],
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://r17-in-memory-only",
    ) as client:
        response = await client.post(
            "/v1/workers/register",
            headers={"authorization": f"Bearer {WORKER}"}, json=body,
        )
        assert response.status_code == 201, response.text
        assert response.json()["serving"] == serving.to_dict()
        # An ONLINE Worker is not eligible for "recovery", which must fail
        # at the repository fence/eligibility check rather than 500.
        unauthorized_recovery = await client.post(
            "/v1/workers/register",
            headers={"authorization": f"Bearer {WORKER}"},
            json={**body, "recovery": True},
        )
        assert unauthorized_recovery.status_code == 409
    observed = app.state.lab_repository.get_worker(LAB_WORKER)
    assert observed.serving.runtime_instance.epoch == serving.runtime_instance.epoch
