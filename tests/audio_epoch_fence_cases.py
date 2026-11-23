"""Shared direct-repository cases for negative-only audio epoch tombstones."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tsumgi.control import (
    ConflictError, JobSubmission, NoCompatibleDeployment, NotFoundError,
    WorkerHeartbeat, WorkerRegistration, WorkerState,
)
from tsumgi.execution import JobResult
from test_audio_artifact_authority import (
    EPOCH, NEXT_EPOCH, authorize, credentials, serving_values, setup_claim, snapshot,
)


@pytest.fixture
def clock():
    return SimpleNamespace(now=datetime.now(UTC))


def fence(repo, request_id=None, *, worker_id="owner", epoch=EPOCH, capability="audio.synthesize"):
    binding, _ = serving_values(capability, epoch)
    return repo.fence_audio_epoch(
        worker_id, epoch,
        expected_deployment_revision=binding.deployment_revision,
        expected_serving_contract_revision=binding.serving_contract_revision,
        fence_request_id=request_id or str(uuid4()),
    )


class AudioEpochFenceCases:
    def test_active_epoch_fence_is_readable_idempotent_and_blocks_positive_effects(self, authority_repo, clock):
        repo = authority_repo
        claim = setup_claim(repo, clock.now)
        before = snapshot(repo, claim)
        request = str(uuid4())
        record = fence(repo, request)
        assert record.to_dict()["state"] == "fenced"
        assert record.fence_request_id == request
        assert repo.get_audio_epoch_fence("owner", EPOCH) == record
        assert repo.fence_audio_epoch(
            "owner", EPOCH,
            expected_deployment_revision=record.deployment_revision,
            expected_serving_contract_revision=record.serving_contract_revision,
            fence_request_id=request,
        ) == record
        assert snapshot(repo, claim) == before
        with pytest.raises(ConflictError):
            repo.claim_next_job("owner", runtime_instance_epoch=EPOCH, now=clock.now)
        with pytest.raises(ConflictError):
            repo.heartbeat_worker("owner", WorkerHeartbeat(
                runtime_instance_epoch=EPOCH, active_job_id=claim.job_id,
                lease_token=claim.lease_token,
            ), now=clock.now)
        with pytest.raises(ConflictError):
            repo.complete_job(claim.job_id, JobResult(text="done"),
                              now=clock.now, **{k: v for k, v in credentials(claim).items()
                                               if k != "attempt"})
        with pytest.raises(ConflictError):
            repo.append_job_event(
                claim.job_id, worker_id="owner", lease_token=claim.lease_token,
                runtime_instance_epoch=EPOCH, kind="progress", payload={}, now=clock.now,
            )
        assert snapshot(repo, claim) == before
        # Explicit cancellation remains a cleanup path; it does not erase the tombstone.
        repo.cancel_job(claim.job_id, now=clock.now)
        assert repo.get_audio_epoch_fence("owner", EPOCH) == record

    def test_new_epoch_is_usable_but_old_epoch_remains_fenced(self, authority_repo, clock):
        repo = authority_repo
        claim = setup_claim(repo, clock.now)
        record = fence(repo)
        # Client cancellation is permitted cleanup before a fresh registration.
        repo.cancel_job(claim.job_id, now=clock.now)
        current = repo.get_worker("owner")
        _, serving = serving_values("audio.synthesize", NEXT_EPOCH)
        repo.register_worker(WorkerRegistration(spec=current.spec, serving=serving), now=clock.now)
        with pytest.raises(ConflictError):
            repo.register_worker(WorkerRegistration(spec=current.spec,
                serving=serving_values("audio.synthesize", EPOCH)[1]), now=clock.now)
        assert repo.get_audio_epoch_fence("owner", EPOCH) == record
        # A fresh E2 job is claimable, while the immutable E1 tombstone remains.
        binding, _ = serving_values("audio.synthesize", NEXT_EPOCH)
        queued = repo.submit_job(JobSubmission(
            capability="audio.synthesize", serving=binding,
            deadline_at=clock.now + timedelta(seconds=120),
            max_attempts=3, payload={"synthetic": True},
        ), now=clock.now)
        admitted = repo.claim_next_job("owner", runtime_instance_epoch=NEXT_EPOCH, now=clock.now)
        assert admitted is not None and admitted.job_id == queued.job_id
        assert repo.get_worker("owner").serving.runtime_instance.epoch == NEXT_EPOCH
        assert repo.get_audio_epoch_fence("owner", EPOCH) == record

    def test_wrong_epoch_and_non_audio_are_rejected(self, authority_repo, clock):
        repo = authority_repo
        setup_claim(repo, clock.now)
        with pytest.raises(ConflictError):
            fence(repo, epoch=NEXT_EPOCH)
        with pytest.raises(ConflictError):
            fence(repo, capability="llm.chat")
        with pytest.raises(NotFoundError):
            repo.get_audio_epoch_fence("owner", NEXT_EPOCH)

    def test_missing_worker_is_not_found(self, authority_repo):
        binding, _ = serving_values("audio.synthesize")
        with pytest.raises(NotFoundError, match="^worker not found$"):
            authority_repo.fence_audio_epoch(
                "missing", EPOCH, expected_deployment_revision=binding.deployment_revision,
                expected_serving_contract_revision=binding.serving_contract_revision,
                fence_request_id=str(uuid4()),
            )

    def test_fence_expected_contract_is_bound_to_current_advertisement(self, authority_repo, clock):
        repo = authority_repo
        setup_claim(repo, clock.now)
        binding, _ = serving_values("audio.synthesize")
        with pytest.raises(ConflictError):
            repo.fence_audio_epoch("owner", EPOCH,
                expected_deployment_revision="sha256:" + "0" * 64,
                expected_serving_contract_revision=binding.serving_contract_revision,
                fence_request_id=str(uuid4()))

    def test_idempotency_conflicts_and_global_request_id_cannot_rebind(self, authority_repo, clock):
        repo = authority_repo
        claim = setup_claim(repo, clock.now)
        request = str(uuid4())
        record = fence(repo, request)
        before = repo.get_audio_epoch_fence("owner", EPOCH).to_dict()
        args = dict(expected_deployment_revision=record.deployment_revision,
                    expected_serving_contract_revision=record.serving_contract_revision)
        with pytest.raises(ConflictError):
            repo.fence_audio_epoch("owner", EPOCH, **args, fence_request_id=str(uuid4()))
        with pytest.raises(ConflictError):
            repo.fence_audio_epoch("owner", EPOCH, **{
                **args, "expected_deployment_revision": "sha256:" + "a" * 64,
                "fence_request_id": request,
            })
        _, next_serving = serving_values("audio.synthesize", NEXT_EPOCH)
        current = repo.get_worker("owner")
        repo.cancel_job(claim.job_id, now=clock.now)
        repo.register_worker(WorkerRegistration(spec=current.spec, serving=next_serving), now=clock.now)
        next_binding, _ = serving_values("audio.synthesize", NEXT_EPOCH)
        with pytest.raises(ConflictError):
            repo.fence_audio_epoch("owner", NEXT_EPOCH,
                expected_deployment_revision=next_binding.deployment_revision,
                expected_serving_contract_revision=next_binding.serving_contract_revision,
                fence_request_id=request,
            )
        assert repo.get_audio_epoch_fence("owner", EPOCH).to_dict() == before

    @pytest.mark.parametrize("bad_request", ["not-uuid", "12345678-1234-4234-9234-123456789ABC", ""])
    def test_invalid_request_identifier_does_not_create_a_fence(self, authority_repo, clock, bad_request):
        repo = authority_repo
        setup_claim(repo, clock.now)
        binding, _ = serving_values("audio.synthesize")
        with pytest.raises(ValueError):
            repo.fence_audio_epoch("owner", EPOCH,
                expected_deployment_revision=binding.deployment_revision,
                expected_serving_contract_revision=binding.serving_contract_revision,
                fence_request_id=bad_request)
        with pytest.raises(NotFoundError):
            repo.get_audio_epoch_fence("owner", EPOCH)

    def test_worker_state_and_inactive_heartbeat_are_denied_after_fence(self, authority_repo, clock):
        repo = authority_repo
        claim = setup_claim(repo, clock.now)
        fence(repo)
        before = snapshot(repo, claim)
        with pytest.raises(ConflictError):
            repo.heartbeat_worker("owner", WorkerHeartbeat(runtime_instance_epoch=EPOCH), now=clock.now)
        with pytest.raises(ConflictError):
            repo.set_worker_state("owner", WorkerState.OFFLINE, runtime_instance_epoch=EPOCH, now=clock.now)
        assert snapshot(repo, claim) == before

    def test_fail_and_artifact_authority_are_denied_without_disclosure(self, authority_repo, clock):
        repo = authority_repo
        claim = setup_claim(repo, clock.now)
        fence(repo)
        before = snapshot(repo, claim)
        with pytest.raises(ConflictError):
            repo.fail_job(claim.job_id, "synthetic", retryable=True, now=clock.now,
                          **{k: v for k, v in credentials(claim).items() if k != "attempt"})
        with pytest.raises(ConflictError) as caught:
            authorize(repo, claim)
        for secret in (claim.lease_token, EPOCH, claim.job_id, "private-audio-payload"):
            assert secret not in str(caught.value)
        assert snapshot(repo, claim) == before

    def test_unfenced_audio_keeps_ordinary_renewal_and_artifactless_completion(self, authority_repo, clock):
        from tsumgi.control import WorkerState

        repo = authority_repo
        claim = setup_claim(repo, clock.now)
        assert authorize(repo, claim) is None
        repo.set_worker_state("owner", WorkerState.OFFLINE, runtime_instance_epoch=EPOCH, now=clock.now)
        repo.heartbeat_worker("owner", WorkerHeartbeat(
            runtime_instance_epoch=EPOCH, active_job_id=claim.job_id,
            lease_token=claim.lease_token,
        ), now=clock.now)
        completed = repo.complete_job(claim.job_id, JobResult(text="synthetic"),
            worker_id="owner", lease_token=claim.lease_token,
            runtime_instance_epoch=EPOCH, now=clock.now)
        assert completed.status.value == "succeeded"

    def test_only_fenced_matching_worker_is_not_admitted(self, authority_repo, clock):
        from tsumgi.control import WorkerState

        repo = authority_repo
        setup_claim(repo, clock.now)
        fence(repo)
        repo.set_worker_state("other", WorkerState.OFFLINE, runtime_instance_epoch=EPOCH, now=clock.now)
        binding, _ = serving_values("audio.synthesize")
        with pytest.raises(NoCompatibleDeployment):
            repo.submit_job(JobSubmission(
                capability="audio.synthesize", serving=binding,
                deadline_at=clock.now + timedelta(seconds=120),
                max_attempts=3, payload={"synthetic": True},
            ), now=clock.now)
        with pytest.raises(ConflictError, match="^audio epoch is fenced$"):
            repo.claim_next_job("owner", runtime_instance_epoch=EPOCH, now=clock.now)

    def test_unbound_worker_is_not_fenceable(self, authority_repo, clock):
        from tsumgi import ResourceShape, WorkerSpec

        spec = WorkerSpec(
            worker_id="unbound", worker_class="cpu-test", resources=ResourceShape(),
            capabilities=frozenset({"audio.synthesize"}),
        )
        authority_repo.register_worker(WorkerRegistration(spec=spec), now=clock.now)
        binding, _ = serving_values("audio.synthesize")
        with pytest.raises(ConflictError):
            authority_repo.fence_audio_epoch(
                "unbound", EPOCH, expected_deployment_revision=binding.deployment_revision,
                expected_serving_contract_revision=binding.serving_contract_revision,
                fence_request_id=str(uuid4()),
            )

    def test_mixed_capabilities_and_multiple_contracts_are_not_fenceable(self, authority_repo, clock):
        from dataclasses import replace

        from tsumgi import ResourceShape, WorkerSpec

        repo = authority_repo
        _, serving = serving_values("audio.synthesize")
        other_contract = serving_values("audio.transcribe")[1].contracts[0]
        mixed = WorkerSpec(
            worker_id="owner", worker_class="cpu-test", resources=ResourceShape(),
            capabilities=frozenset({"audio.synthesize", "llm.chat"}),
        )
        repo.register_worker(WorkerRegistration(spec=mixed, serving=serving), now=clock.now)
        with pytest.raises(ConflictError):
            fence(repo)
        baseline = WorkerSpec(
            worker_id="owner", worker_class="cpu-test", resources=ResourceShape(),
            capabilities=frozenset({"audio.synthesize", "audio.transcribe"}),
        )
        two = replace(serving, contracts=(*serving.contracts, other_contract))
        repo.register_worker(WorkerRegistration(spec=baseline, serving=two), now=clock.now)
        with pytest.raises(ConflictError):
            fence(repo)
        with pytest.raises(NotFoundError):
            repo.get_audio_epoch_fence("owner", EPOCH)

    def test_serving_downgrade_and_recovered_epoch_replay_are_rejected(self, authority_repo, clock):
        repo = authority_repo
        claim = setup_claim(repo, clock.now)
        record = fence(repo)
        with pytest.raises(ConflictError):
            repo.register_worker(WorkerRegistration(spec=repo.get_worker("owner").spec, serving=None),
                                 now=clock.now)
        repo.recover_expired_jobs(now=claim.lease_expires_at)
        repo.expire_stale_workers(now=clock.now + timedelta(seconds=301))
        with pytest.raises(ConflictError):
            repo.register_worker(WorkerRegistration(
                spec=repo.get_worker("owner").spec,
                serving=serving_values("audio.synthesize", EPOCH)[1],
            ), recovery=True, now=clock.now + timedelta(seconds=301))
        assert repo.get_audio_epoch_fence("owner", EPOCH) == record

    @pytest.mark.parametrize("field,value", [
        ("runtime_instance_epoch", "not-a-uuid"),
        ("expected_deployment_revision", "SHA256:" + "a" * 64),
        ("expected_serving_contract_revision", "sha256:" + "A" * 64),
        ("worker_id", "x" * 257),
    ])
    def test_malformed_fence_arguments_are_rejected_without_mutation(
        self, authority_repo, clock, field, value,
    ):
        repo = authority_repo
        setup_claim(repo, clock.now)
        binding, _ = serving_values("audio.synthesize")
        args = dict(
            worker_id="owner", runtime_instance_epoch=EPOCH,
            expected_deployment_revision=binding.deployment_revision,
            expected_serving_contract_revision=binding.serving_contract_revision,
            fence_request_id=str(uuid4()),
        )
        args[field] = value
        with pytest.raises(ValueError):
            repo.fence_audio_epoch(**args)
        with pytest.raises(NotFoundError):
            repo.get_audio_epoch_fence("owner", EPOCH)
