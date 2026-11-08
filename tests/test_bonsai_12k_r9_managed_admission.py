"""#232 R9: synthetic-only opt-in Managed admission and old 8K isolation.

This file deliberately uses fake hashes and an in-memory Control. A loadable
synthetic Serving identity is NOT a verified runtime or deployment artifact.
No host, GPU, model, filesystem, real PostgreSQL or network is contacted.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import timedelta

import pytest
from bonsai_12k_spica_candidate import (
    LIMITS_8K,
    LIMITS_12K,
    PROFILE_8K,
    PROFILE_12K,
    build_spica_unqualified_candidate,
)
from test_bonsai_12k_spica_candidate import digest, make_inputs

from astrumweaver import ResourceShape, WorkerSpec
from astrumweaver.control import (
    ConflictError,
    InMemoryControlRepository,
    JobStatus,
    JobSubmission,
    NoCompatibleDeployment,
    WorkerRegistration,
    utc_now,
)
from astrumweaver.gateway.chat import ChatGatewayError, ChatProfileCatalog
from astrumweaver.serving import (
    DeploymentIdentity,
    LogicalServingProfile,
    ServingContract,
    ServingContractError,
    ServingDeploymentDeclaration,
    resolve_profile,
)

# Fake, non-attested SHA shapes used ONLY in the in-memory test process.
FAKE_EXECUTION = digest("a")
FAKE_EVIDENCE = digest("b")
OLD_EPOCH = "11111111-1111-4111-8111-111111111111"
NEW_EPOCH = "22222222-2222-4222-8222-222222222222"


def synthetic_only_chain():
    """Build domain-valid fake values without returning deployable JSON files."""
    source = make_inputs()
    before = deepcopy(source)
    draft = build_spica_unqualified_candidate(**source)
    invalid_serving = deepcopy(draft["serving_INVALID_draft"])
    invalid_catalog = deepcopy(draft["control_INVALID_draft"])

    deployment_dict = dict(invalid_serving["deployment"])
    deployment_dict["execution_config_sha256"] = FAKE_EXECUTION
    deployment = DeploymentIdentity(**deployment_dict)

    contract_dict = dict(invalid_serving["contracts"][0]["contract"])
    contract_dict["deployment_revision"] = deployment.revision
    contract_dict["validation_evidence_sha256"] = FAKE_EVIDENCE
    contract = ServingContract(**contract_dict)
    declaration = ServingDeploymentDeclaration(deployment, (contract,))

    new_row = invalid_catalog["profiles"][-1]
    assert new_row["profile"]["profile_id"] == PROFILE_12K
    profile_dict = new_row["profile"]
    profile_dict["deployment_revision"] = deployment.revision
    profile_dict["serving_contract_revision"] = contract.revision
    profile = LogicalServingProfile(**profile_dict)
    new_row["deployment"] = deployment.to_dict()
    new_row["contract"] = contract.to_dict()
    new_row["profile"] = profile.to_dict()
    catalog = ChatProfileCatalog.from_dict(invalid_catalog)

    assert before == source
    assert draft["admission"] == "DENIED_PENDING_REAL_CANARY_AND_REVIEW"
    return source, draft, declaration, catalog, invalid_catalog


def register(repo, worker_id, declaration, epoch):
    return repo.register_worker(WorkerRegistration(
        spec=WorkerSpec(
            worker_id=worker_id, worker_class="synthetic-offline-only",
            resources=ResourceShape(), capabilities=frozenset({"llm.chat"}),
        ),
        serving=declaration.advertisement(epoch=epoch),
    ))


def submission_for(catalog, profile_id=PROFILE_12K, *, key="synthetic-12k"):
    return JobSubmission(
        capability="llm.chat",
        payload={"schema_version": "fake-not-a-native-request"},
        serving=catalog.get(profile_id).binding,
        deadline_at=utc_now() + timedelta(seconds=30),
        max_attempts=1,
        idempotency_key=key,
    )


def test_synthetic_chain_opt_in_only_and_existing_8k_qwen_unchanged():
    source, draft, declaration, catalog, raw = synthetic_only_chain()
    assert [row.profile_id for row in catalog.profiles] == [
        "qwen3.8-27b", PROFILE_8K, PROFILE_12K,
    ]
    assert raw["profiles"][:2] == source["control_catalog"]["profiles"]
    assert dict(catalog.get(PROFILE_8K).resolved.effective_limits) == LIMITS_8K
    assert dict(catalog.get(PROFILE_12K).resolved.effective_limits) == LIMITS_12K
    old = ServingDeploymentDeclaration.from_dict(source["spica_serving"])
    assert declaration.deployment.revision != old.deployment.revision
    assert declaration.contracts[0].revision != old.contracts[0].revision
    assert catalog.get(PROFILE_12K).resolved.deployment == declaration.deployment
    assert catalog.get(PROFILE_12K).resolved.contract == declaration.contracts[0]
    assert draft["source_gpu_uuids"]["spica_worker"] != draft["source_gpu_uuids"]["deneb_worker"]
    assert draft["runtime_UNQUALIFIED_draft"]["setup_intent"]["configuration"]["gpu_uuids"] == [
        draft["source_gpu_uuids"]["spica_worker"]
    ]
    assert declaration.deployment.execution_config_sha256 == FAKE_EXECUTION
    assert declaration.contracts[0].validation_evidence_sha256 == FAKE_EVIDENCE


@pytest.mark.parametrize(("limit", "maximum"), tuple(LIMITS_12K.items()))
def test_synthetic_12k_limit_boundaries_fail_closed(limit, maximum):
    _, _, _, catalog, _ = synthetic_only_chain()
    resolved = catalog.get(PROFILE_12K).resolved
    assert resolve_profile(
        resolved.profile, resolved.contract, resolved.deployment,
        requested_usage={limit: maximum},
    ).profile_revision == resolved.profile_revision
    with pytest.raises(ServingContractError, match="limit-exceeded"):
        resolve_profile(
            resolved.profile, resolved.contract, resolved.deployment,
            requested_usage={limit: maximum + 1},
        )


@pytest.mark.parametrize("part", ["deployment", "contract", "profile"])
def test_stale_8k_identity_cannot_be_attached_to_new_12k_row(part):
    source, _, _, _, raw = synthetic_only_chain()
    row = deepcopy(raw["profiles"][-1])
    old = source["control_catalog"]["profiles"][1]
    row[part] = deepcopy(old[part])
    if part == "profile":
        # Keep the opt-in ID unique: exercise revision fencing rather than
        # accidentally passing this test on a duplicate profile ID.
        row["profile"]["profile_id"] = PROFILE_12K
    changed = deepcopy(raw)
    changed["profiles"][-1] = row
    with pytest.raises((ChatGatewayError, ServingContractError)):
        ChatProfileCatalog.from_dict(changed)


def test_invalid_r0_draft_is_not_a_serving_or_catalog_manifest():
    _, draft, _, _, _ = synthetic_only_chain()
    with pytest.raises(ServingContractError):
        ServingDeploymentDeclaration.from_dict(draft["serving_INVALID_draft"])
    with pytest.raises(ChatGatewayError):
        ChatProfileCatalog.from_dict(draft["control_INVALID_draft"])


def test_deneb_8k_and_spica_8k_cannot_admit_or_claim_opt_in_12k():
    source, _, synthetic, catalog, _ = synthetic_only_chain()
    old = ServingDeploymentDeclaration.from_dict(source["spica_serving"])
    repo = InMemoryControlRepository()
    register(repo, "bonsai-deneb", old, OLD_EPOCH)
    register(repo, "bonsai-spica", old, OLD_EPOCH)
    new = submission_for(catalog)
    with pytest.raises(NoCompatibleDeployment):
        repo.submit_job(new)
    assert repo.list_jobs() == []
    assert repo.get_worker("bonsai-deneb").active_jobs == 0
    assert repo.get_worker("bonsai-spica").active_jobs == 0

    # Simulated separately prepared new instance/epoch, NOT an actual Worker.
    # Same physical Worker ID would require an external drain/process/GPU gate.
    register(repo, "synthetic-spica-new-epoch", synthetic, NEW_EPOCH)
    with pytest.raises(ConflictError):
        repo.claim_next_job("synthetic-spica-new-epoch", runtime_instance_epoch=OLD_EPOCH)
    accepted = repo.submit_job(new)
    assert accepted.status is JobStatus.QUEUED
    assert accepted.serving == catalog.get(PROFILE_12K).binding
    assert repo.claim_next_job("bonsai-deneb", runtime_instance_epoch=OLD_EPOCH) is None
    assert repo.claim_next_job("bonsai-spica", runtime_instance_epoch=OLD_EPOCH) is None
    claimed = repo.claim_next_job(
        "synthetic-spica-new-epoch", runtime_instance_epoch=NEW_EPOCH,
    )
    assert claimed is not None
    assert claimed.claimed_runtime_instance_epoch == NEW_EPOCH
    assert claimed.claimed_deployment_revision == synthetic.deployment.revision
    assert claimed.claimed_serving_contract_revision == synthetic.contracts[0].revision
    repo.cancel_job(claimed.job_id)
    assert repo.get_job(claimed.job_id).status is JobStatus.CANCELLED


def test_8k_old_profile_remains_separate_and_not_rebound_to_12k():
    source, _, synthetic, catalog, _ = synthetic_only_chain()
    old = ServingDeploymentDeclaration.from_dict(source["deneb_serving"])
    repo = InMemoryControlRepository()
    register(repo, "bonsai-deneb", old, OLD_EPOCH)
    register(repo, "synthetic-spica-new-epoch", synthetic, NEW_EPOCH)
    old_job = repo.submit_job(submission_for(catalog, PROFILE_8K, key="old-8k"))
    assert old_job.serving == catalog.get(PROFILE_8K).binding
    assert repo.claim_next_job(
        "synthetic-spica-new-epoch", runtime_instance_epoch=NEW_EPOCH,
    ) is None
    claimed = repo.claim_next_job("bonsai-deneb", runtime_instance_epoch=OLD_EPOCH)
    assert claimed is not None and claimed.job_id == old_job.job_id
    repo.cancel_job(claimed.job_id)


def test_synthetic_same_spica_id_requires_zero_active_jobs_and_fresh_epoch():
    """Control-level guard only; cannot prove Native child/GPU released."""
    source, _, synthetic, catalog, _ = synthetic_only_chain()
    old = ServingDeploymentDeclaration.from_dict(source["spica_serving"])
    repo = InMemoryControlRepository()
    register(repo, "bonsai-spica", old, OLD_EPOCH)
    register(repo, "bonsai-deneb", old, OLD_EPOCH)

    # The old 8K Spica is actually busy with its old revision in memory.
    original = repo.submit_job(
        submission_for(catalog, PROFILE_8K, key="in-flight-old-spica"),
    )
    ongoing = repo.claim_next_job("bonsai-spica", runtime_instance_epoch=OLD_EPOCH)
    assert ongoing is not None and ongoing.job_id == original.job_id
    with pytest.raises(ConflictError, match="cannot change while jobs are active"):
        register(repo, "bonsai-spica", synthetic, NEW_EPOCH)

    repo.cancel_job(original.job_id)
    assert repo.get_job(original.job_id).status is JobStatus.CANCELLED
    updated = register(repo, "bonsai-spica", synthetic, NEW_EPOCH)
    assert updated.serving is not None
    assert updated.serving.runtime_instance.epoch == NEW_EPOCH
    with pytest.raises(ConflictError, match="stale"):
        repo.claim_next_job("bonsai-spica", runtime_instance_epoch=OLD_EPOCH)

    next_job = repo.submit_job(submission_for(catalog, key="fresh-spica-12k"))
    assert next_job.status is JobStatus.QUEUED
    assert repo.claim_next_job("bonsai-deneb", runtime_instance_epoch=OLD_EPOCH) is None
    next_claim = repo.claim_next_job("bonsai-spica", runtime_instance_epoch=NEW_EPOCH)
    assert next_claim is not None and next_claim.job_id == next_job.job_id
    assert next_claim.claimed_deployment_revision == synthetic.deployment.revision
    repo.cancel_job(next_claim.job_id)

    # Re-registering same-ID proves ONLY Control matching: no real Worker
    # drain, stop, old GPU-owner release or new inference is performed.
