"""Memory rehearsal for strict per-worker identity authorization."""
from __future__ import annotations

import pytest

from astrumweaver.control import InMemoryControlRepository
from audio_epoch_acceptance import E1, make_harness, snapshot_repository
from worker_identity_cases import OWN, WORKERS, WORKER_ID, WorkerIdentityCases


@pytest.fixture
def identity_harness(tmp_path, monkeypatch):
    repository = InMemoryControlRepository(worker_ttl_seconds=300, lease_seconds=60)

    def observer():
        return snapshot_repository(repository, [job.job_id for job in repository.list_jobs()])

    with make_harness(
        tmp_path, monkeypatch, database_url="postgresql://unused.invalid/memory-rehearsal",
        observer=observer, memory_repository=repository, worker_credentials=WORKERS,
        worker_token=OWN,
    ) as harness:
        yield harness


class TestMemoryWorkerIdentity(WorkerIdentityCases):
    """Explicit in-memory substitution, not PostgreSQL evidence."""

    @pytest.mark.asyncio
    async def test_control_client_keeps_worker_wire_compatibility(self, identity_harness):
        import httpx

        from astrumweaver.worker.client import ControlClient, ControlTransportError
        from astrumweaver.execution import JobResult
        from astrumweaver.transport import worker_registration_from_dict
        from audio_epoch_acceptance import registration

        h = identity_harness
        proposal = worker_registration_from_dict(registration(h.profile, E1))
        transport = httpx.ASGITransport(app=h.client.app)
        async with ControlClient("http://test", OWN, transport=transport) as client:
            registered = await client.register(
                spec=proposal.spec, max_concurrency=1, serving=proposal.serving,
            )
            assert registered["spec"]["worker_id"] == WORKER_ID
            queued = h.submit()
            claim = await client.claim(WORKER_ID, runtime_instance_epoch=E1)
            assert claim is not None and claim.request.job_id == queued["job_id"]
            with pytest.raises(ControlTransportError) as caught:
                await client.claim("identity-other", runtime_instance_epoch=E1)
            assert caught.value.status_code == 403
            await client.heartbeat(WORKER_ID, active_job_id=queued["job_id"],
                                   lease_token=claim.lease_token, runtime_instance_epoch=E1)
            result = await client.complete(WORKER_ID, queued["job_id"], claim.lease_token,
                                           JobResult(text="synthetic"), runtime_instance_epoch=E1)
            assert result["status"] == "succeeded"
