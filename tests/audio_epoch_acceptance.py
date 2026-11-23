"""Shared HTTP oracle for #247 preparation, NOT external-engine qualification.

Both backends use the standard daemon assembly and both audio schemas. Memory
is an explicitly named rehearsal, never a fallback for PostgreSQL acceptance.
All voices, pins, text and WAV bytes are synthetic; no provider is started.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import struct
from typing import Callable

from fastapi.testclient import TestClient

from tsumgi import ResourceShape, WorkerSpec
from tsumgi.control import daemon
from tsumgi.control import NotFoundError
from tsumgi.control.serde import worker_spec_to_dict
from tsumgi.gateway.audio import AudioProfileCatalog
from tsumgi.serving import RuntimeInstance, WorkerServingAdvertisement
from tsumgi.transport import (
    PROTOCOL_VERSION, SERVING_EXTENSION, JOB_EVENTS_EXTENSION, job_event_record_to_dict,
    job_record_to_dict, worker_record_to_dict,
)
from test_audio_voice_api import make_profile

E1 = "12345678-1234-4234-9234-123456789abc"
E2 = "22345678-1234-4234-9234-123456789abc"
WORKER_ID = "epoch-acceptance-synthetic"
WORKER_TOKEN = "epoch-synthetic-worker-token"
CLIENT_TOKEN = "epoch-synthetic-control-token"
AUDIO_TOKEN = "epoch-synthetic-audio-token"
OPERATOR_TOKEN = "epoch-synthetic-operator-token"
FENCE_REQUEST = "32345678-1234-4234-9234-123456789abc"
WORKER = {"authorization": "Bearer " + WORKER_TOKEN}
CLIENT = {"authorization": "Bearer " + CLIENT_TOKEN}
OPERATOR = {"authorization": "Bearer " + OPERATOR_TOKEN}
WAV = (b"RIFF" + struct.pack("<I", 38) + b"WAVEfmt "
       + struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16)
       + b"data" + struct.pack("<I", 2) + b"\x00\x00")
BASE = f"/v1/workers/{WORKER_ID}"


def snapshot_repository(repository, job_ids) -> dict:
    """Read actual stored records, not request counters or response assertions."""
    identifiers = sorted(job_ids)
    assert len(identifiers) <= 128
    workers = repository.list_workers()
    fences = {}
    for worker in workers:
        for epoch in (E1, E2):
            try:
                record = repository.get_audio_epoch_fence(worker.worker_id, epoch)
            except NotFoundError:
                continue
            fences[worker.worker_id + ":" + epoch] = record.to_dict()
    return {
        "workers": [worker_record_to_dict(w) for w in workers],
        "fences": fences,
        "jobs": {job_id: job_record_to_dict(repository.get_job(job_id)) for job_id in identifiers},
        "events": {
            job_id: [job_event_record_to_dict(e) for e in repository.list_job_events(job_id)]
            for job_id in identifiers
        },
    }


def envelope(epoch=None, **values):
    return {
        "protocol_version": PROTOCOL_VERSION, "extensions": [SERVING_EXTENSION],
        **({"runtime_instance_epoch": epoch} if epoch is not None else {}), **values,
    }


def registration(profile, epoch, *, max_concurrency=1):
    resolved = profile.resolved
    spec = WorkerSpec(
        worker_id=WORKER_ID, worker_class="synthetic-cpu", resources=ResourceShape(),
        capabilities=frozenset({resolved.profile.capability}),
    )
    serving = WorkerServingAdvertisement(
        deployment_revision=resolved.deployment.revision,
        runtime_instance=RuntimeInstance(resolved.deployment.revision, epoch),
        contracts=(resolved.contract,),
    )
    return envelope(spec=worker_spec_to_dict(spec), max_concurrency=max_concurrency,
                    serving=serving.to_dict())


@dataclass
class EpochHarness:
    client: TestClient
    observer: Callable[[], dict]
    profile: object
    output_root: Path
    worker_headers: dict = field(default_factory=lambda: dict(WORKER))

    def readback(self):
        return self.observer()

    def worker_epoch(self):
        workers = self.readback()["workers"]
        assert len(workers) == 1
        return workers[0]["serving"]["runtime_instance"]["epoch"]

    def effects(self):
        files = {
            p.relative_to(self.output_root).as_posix(): (
                p.stat().st_mode, p.stat().st_size, hashlib.sha256(p.read_bytes()).hexdigest(),
            )
            for p in self.output_root.rglob("*") if p.is_file()
        }
        return self.readback(), files

    def register(self, epoch, *, max_concurrency=1):
        return self.client.post("/v1/workers/register", headers=self.worker_headers,
                                json=registration(self.profile, epoch, max_concurrency=max_concurrency))

    def fence_body(self, request_id=FENCE_REQUEST):
        return {
            "protocol_version": PROTOCOL_VERSION,
            "expected_deployment_revision": self.profile.resolved.deployment.revision,
            "expected_serving_contract_revision": self.profile.resolved.contract.revision,
            "fence_request_id": request_id,
        }

    def fence(self, epoch=E1, request_id=FENCE_REQUEST):
        return self.client.post(
            f"/v1/admin/audio-epochs/{WORKER_ID}/{epoch}/fence", headers=OPERATOR,
            json=self.fence_body(request_id),
        )

    def read_fence(self, epoch=E1):
        return self.client.get(f"/v1/admin/audio-epochs/{WORKER_ID}/{epoch}", headers=OPERATOR)

    def assert_fenced(self, epoch=E1):
        response = self.fence(epoch)
        assert response.status_code == 200, response.text
        receipt = response.json()
        assert receipt["state"] == "fenced" and receipt["record_revision"] == 1
        assert receipt["runtime_instance_epoch"] == epoch
        assert receipt["schema_version"] == "audio-epoch-fence-v1"
        assert response.headers["cache-control"] == "no-store"
        expected = {key: value for key, value in receipt.items() if key != "protocol_version"}
        assert self.readback()["fences"][WORKER_ID + ":" + epoch] == expected
        assert self.read_fence(epoch).json() == receipt
        return receipt

    def submit(self):
        result = self.client.post("/v1/jobs", headers=CLIENT, json=envelope(
            capability="audio.synthesize", serving=self.profile.binding.to_dict(),
            deadline_at=(datetime.now(UTC) + timedelta(seconds=300)).isoformat(),
            max_attempts=3, payload={"input": "synthetic epoch acceptance"},
        ))
        assert result.status_code == 201, result.text
        return result.json()

    def claim(self, epoch):
        return self.client.post(BASE + "/jobs/claim", headers=self.worker_headers, json=envelope(epoch))

    def state(self, epoch, state):
        return self.client.post(BASE + "/state", headers=self.worker_headers, json=envelope(epoch, state=state))

    def renew(self, claim, epoch, *, state=None):
        return self.client.post(BASE + "/heartbeat", headers=self.worker_headers, json=envelope(
            epoch, active_job_id=claim["job_id"], lease_token=claim["lease_token"],
            **({"state": state} if state is not None else {}),
        ))

    def stage(self, claim, epoch):
        return self.client.post(BASE + f"/jobs/{claim['job_id']}/audio/output", content=WAV, headers={
            **self.worker_headers, "content-type": "audio/wav", "x-audio-attempt": str(claim["attempts"]),
            "x-audio-lease-token": claim["lease_token"], "x-audio-runtime-epoch": epoch,
        })

    def complete(self, claim, epoch, references=()):
        return self.client.post(BASE + f"/jobs/{claim['job_id']}/complete", headers=self.worker_headers,
                                json=envelope(
                                    epoch, lease_token=claim["lease_token"],
                                    result={"text": None, "artifacts": list(references)},
                                ))

    def publication(self, claim, reference):
        artifact_id = reference["uri"].split(":", 1)[1]
        return self.client.get(
            f"/v1/jobs/{claim['job_id']}/audio/output/{artifact_id}", headers=CLIENT,
        )

    def denied(self, operation, status=409):
        before = self.effects()
        result = operation()
        assert result.status_code == status, result.text
        assert self.effects() == before, "rejected request changed durable state or staged files"
        for secret in (WORKER_TOKEN, CLIENT_TOKEN, AUDIO_TOKEN, OPERATOR_TOKEN):
            assert secret not in result.text
        assert self.worker_headers["authorization"].removeprefix("Bearer ") not in result.text
        for record in before[0]["jobs"].values():
            if record["lease_token"]:
                assert record["lease_token"] not in result.text
        return result

    def seed_claim(self, *, max_concurrency=1):
        assert self.register(E1, max_concurrency=max_concurrency).status_code == 201
        queued = self.submit()
        response = self.claim(E1)
        assert response.status_code == 200, response.text
        claimed = response.json()
        assert claimed["job_id"] == queued["job_id"]
        assert claimed["attempts"] == 1 and claimed["claimed_runtime_instance_epoch"] == E1
        assert claimed["serving"] == self.profile.binding.to_dict()
        assert claimed["claimed_deployment_revision"] == self.profile.resolved.deployment.revision
        assert claimed["claimed_serving_contract_revision"] == self.profile.resolved.contract.revision
        assert claimed["assigned_worker_id"] == WORKER_ID and claimed["lease_token"]
        stored = self.readback()["jobs"][claimed["job_id"]]
        assert stored == claimed
        return claimed


@contextmanager
def make_harness(tmp_path, monkeypatch, *, database_url, observer, memory_repository=None,
                 fencing_enabled=True, worker_credentials=None, worker_token=WORKER_TOKEN,
                 preparation_enabled=False, preparation_ttl_seconds=60):
    """Production build_app + lifespan; ASGI transport, not a TCP/host trial."""
    profile = make_profile("model-tts")
    catalog = AudioProfileCatalog((profile, make_profile("model-stt")))
    catalog_path = tmp_path / "synthetic-catalog.json"
    catalog_path.write_text(json.dumps(catalog.to_dict()), encoding="utf-8")
    roots = [tmp_path / name for name in ("input", "output")]
    for root in roots:
        root.mkdir(mode=0o700)
    monkeypatch.setenv("TSUMGI_DATABASE_URL", database_url)
    worker_auth_config = ""
    if worker_credentials is None:
        monkeypatch.setenv("TSUMGI_WORKER_TOKEN", worker_token)
    else:
        monkeypatch.delenv("TSUMGI_WORKER_TOKEN", raising=False)
        credentials_path = tmp_path / "worker-credentials.json"
        with os.fdopen(os.open(credentials_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as handle:
            json.dump({"schema_version": "worker-credentials-v1", "workers": worker_credentials}, handle)
        worker_auth_config = (
            '[worker_auth]\nmode = "per-worker"\ncredentials_file = '
            + json.dumps(str(credentials_path)) + "\n"
        )
    monkeypatch.setenv("TSUMGI_CLIENT_TOKEN", CLIENT_TOKEN)
    monkeypatch.setenv("TSUMGI_AUDIO_CLIENT_TOKEN", AUDIO_TOKEN)
    monkeypatch.setenv("TSUMGI_AUDIO_EPOCH_OPERATOR_TOKEN", OPERATOR_TOKEN)
    if memory_repository is not None:
        # Only the explicitly labelled memory rehearsal uses this substitution.
        monkeypatch.setattr(daemon, "PostgresControlRepository", lambda *a, **k: memory_repository)
    config = tmp_path / "control.toml"
    config.write_text(f"""
[control]
client_auth = "bearer"
worker_ttl_seconds = 300
lease_seconds = 60
maintenance_interval_seconds = 3600.0
db_pool_min_size = 0
db_pool_max_size = 2
{worker_auth_config}
[audio_epoch_fencing]
enabled = {str(fencing_enabled).lower()}
[audio_epoch_preparation]
enabled = {str(preparation_enabled).lower()}
ttl_seconds = {int(preparation_ttl_seconds)}
[audio_gateway]
enabled = true
catalog = {json.dumps(str(catalog_path))}
[audio_gateway.ingress]
request_bytes = 2048
input_bytes = 128
file_bytes = 1024
metadata_bytes = 256
part_header_bytes = 256
total_header_bytes = 2048
receive_events = 16
idle_timeout_seconds = 0.5
total_timeout_seconds = 1.0
[audio_gateway.speech]
output_bytes = 512
request_timeout_seconds = 4.0
max_pending_admissions = 2
poll_interval_seconds = 0.01
[audio_gateway.transcriptions]
text_bytes = 128
request_timeout_seconds = 4.0
max_pending_admissions = 1
poll_interval_seconds = 0.01
[audio_gateway.pcm]
max_decoded_bytes = 128
max_duration_seconds = 1
[audio_gateway.input_spool]
root = {json.dumps(str(roots[0]))}
ttl_seconds = 600.0
max_total_bytes = 4096
max_artifacts = 4
[audio_gateway.output_spool]
root = {json.dumps(str(roots[1]))}
ttl_seconds = 600.0
max_total_bytes = 4096
max_artifacts = 4
""", encoding="utf-8")
    initial = observer()
    assert {key: initial[key] for key in ("workers", "jobs", "events")} == {
        "workers": [], "jobs": {}, "events": {},
    }
    app = daemon.build_app(str(config))
    assert {"/v1/audio/speech", "/v1/audio/transcriptions", "/v1/audio/voices"} <= set(
        app.openapi()["paths"],
    )
    with TestClient(app) as client:
        assert client.get("/v1/ready").status_code == 200
        yield EpochHarness(client, observer, profile, roots[1],
                           {"authorization": "Bearer " + worker_token})


class EpochAcceptanceCases:
    """Current binding plus explicit, negative-only epoch revocation."""

    def test_e1_durable_binding_and_unpublished_stage(self, epoch_harness):
        h = epoch_harness
        claim = h.seed_claim()
        assert h.worker_epoch() == E1
        stage = h.stage(claim, E1)
        assert stage.status_code == 201, stage.text
        before = h.effects()
        assert h.publication(claim, stage.json()).status_code == 404
        assert h.effects() == before
        assert h.readback()["jobs"][claim["job_id"]]["result"] is None

    def test_offline_rejects_new_claim_and_staged_wav_publication(self, epoch_harness):
        h = epoch_harness
        claim = h.seed_claim(max_concurrency=2)
        queued = h.submit()
        staged = h.stage(claim, E1)
        assert staged.status_code == 201
        reference = staged.json()
        eligible = h.readback()
        assert eligible["workers"][0]["state"] == "online"
        assert eligible["workers"][0]["active_jobs"] == 1
        assert eligible["workers"][0]["max_concurrency"] == 2
        assert eligible["jobs"][queued["job_id"]]["status"] == "queued"
        assert h.state(E1, "offline").status_code == 200
        assert h.readback()["workers"][0]["state"] == "offline"
        h.denied(lambda: h.claim(E1), status=204)
        h.denied(lambda: h.stage(claim, E1))
        h.denied(lambda: h.complete(claim, E1, (reference,)))
        assert h.publication(claim, reference).status_code == 404
        # OFFLINE alone neither releases a running attempt nor admits E2.
        h.denied(lambda: h.register(E2, max_concurrency=2))
        # Positive control: only the lifecycle state changes. The queued job
        # and spare slot really were usable, so 204 was not an empty/full queue
        # artifact. This reversibility also must not be called FENCED(E1).
        assert h.state(E1, "online").status_code == 200
        admitted = h.claim(E1)
        assert admitted.status_code == 200
        assert admitted.json()["job_id"] == queued["job_id"]
        assert admitted.json()["claimed_runtime_instance_epoch"] == E1
        assert h.readback()["jobs"][queued["job_id"]] == admitted.json()

    def test_reconnect_rotation_fences_e1_effects_then_only_e2_publishes(self, epoch_harness):
        h = epoch_harness
        first = h.seed_claim()
        staged = h.stage(first, E1)
        assert staged.status_code == 201
        old_ref = staged.json()
        h.denied(lambda: h.register(E2))  # cannot replace an active E1 attempt
        release = h.client.post(BASE + f"/jobs/{first['job_id']}/fail", headers=WORKER,
                                json=envelope(E1, lease_token=first["lease_token"],
                                              error="synthetic reconnect", retryable=True))
        assert release.status_code == 200 and release.json()["status"] == "queued"
        assert h.state(E1, "offline").status_code == 200
        withdrawn = h.readback()
        assert withdrawn["workers"][0]["state"] == "offline"
        assert withdrawn["workers"][0]["active_jobs"] == 0
        assert withdrawn["jobs"][first["job_id"]]["lease_token"] is None
        assert withdrawn["jobs"][first["job_id"]]["claimed_runtime_instance_epoch"] is None
        assert h.publication(first, old_ref).status_code == 404
        # A new sender proposes E2 after explicit attempt release, not a
        # tombstone/qualification grant. Fresh PG observers read every step.
        assert h.register(E2).status_code == 201
        assert h.worker_epoch() == E2
        h.denied(lambda: h.client.post(BASE + "/heartbeat", headers=WORKER,
                                       json=envelope(E1, state="online")))
        h.denied(lambda: h.state(E1, "online"))
        h.denied(lambda: h.claim(E1))
        h.denied(lambda: h.claim(None))
        current = h.claim(E2)
        assert current.status_code == 200
        second = current.json()
        assert second["job_id"] == first["job_id"] and second["attempts"] == 2
        assert second["lease_token"] != first["lease_token"]
        assert second["claimed_runtime_instance_epoch"] == E2
        assert second["serving"] == h.profile.binding.to_dict()
        assert second["claimed_deployment_revision"] == h.profile.resolved.deployment.revision
        assert second["claimed_serving_contract_revision"] == h.profile.resolved.contract.revision
        assert h.readback()["jobs"][second["job_id"]] == second
        # Test both fully stale credentials and E1 with the CURRENT lease and
        # attempt, so a stale-token check cannot masquerade as epoch fencing.
        for claim in (first, second):
            h.denied(lambda claim=claim: h.renew(claim, E1, state="online"))
            h.denied(lambda claim=claim: h.stage(claim, E1))
            h.denied(lambda claim=claim: h.complete(claim, E1))
        before_lease = h.readback()["jobs"][second["job_id"]]["lease_expires_at"]
        assert before_lease is not None
        assert h.renew(second, E2, state="online").status_code == 200
        assert h.readback()["jobs"][second["job_id"]]["lease_expires_at"] > before_lease
        staged = h.stage(second, E2)
        assert staged.status_code == 201
        new_ref = staged.json()
        assert new_ref != old_ref
        h.denied(lambda: h.complete(second, E1, (new_ref,)))
        assert h.publication(second, new_ref).status_code == 404
        complete = h.complete(second, E2, (new_ref,))
        assert complete.status_code == 200 and complete.json()["status"] == "succeeded"
        final = h.readback()
        assert final["workers"][0]["serving"]["runtime_instance"]["epoch"] == E2
        assert final["workers"][0]["active_jobs"] == 0
        assert final["jobs"][second["job_id"]]["status"] == "succeeded"
        assert final["jobs"][second["job_id"]]["claimed_runtime_instance_epoch"] == E2
        assert final["jobs"][second["job_id"]]["result"]["artifacts"] == [new_ref]
        published = h.publication(second, new_ref)
        assert published.status_code == 200 and published.content == WAV
        assert published.headers["cache-control"] == "no-store"
        assert h.publication(first, old_ref).status_code == 404

    def test_fenced_e1_registration_cannot_restore_admission(self, epoch_harness):
        h = epoch_harness
        assert h.register(E1).status_code == 201
        receipt = h.assert_fenced()
        assert h.readback()["workers"][0]["state"] == "online"
        assert h.register(E2).status_code == 201
        assert h.worker_epoch() == E2
        h.submit()
        h.denied(lambda: h.register(E1))
        h.denied(lambda: h.claim(E1))
        assert h.fence().json() == receipt  # exact retry after E2; no new grant
        assert h.read_fence().json() == receipt

    def test_fenced_e1_cannot_renew_by_omitting_heartbeat_state(self, epoch_harness):
        h = epoch_harness
        claim = h.seed_claim()
        h.assert_fenced()
        h.denied(lambda: h.renew(claim, E1))
        h.denied(lambda: h.renew(claim, E1, state="online"))
        h.denied(lambda: h.client.post(BASE + "/heartbeat", headers=WORKER, json=envelope(E1)))
        h.denied(lambda: h.state(E1, "online"))

    def test_fenced_e1_cannot_complete_with_or_without_an_artifact(self, epoch_harness):
        h = epoch_harness
        claim = h.seed_claim()
        stage = h.stage(claim, E1)
        assert stage.status_code == 201
        reference = stage.json()
        h.assert_fenced()
        h.denied(lambda: h.complete(claim, E1))
        h.denied(lambda: h.complete(claim, E1, (reference,)))
        h.denied(lambda: h.stage(claim, E1))
        assert h.publication(claim, reference).status_code == 404
        h.denied(lambda: h.client.post(BASE + f"/jobs/{claim['job_id']}/events", headers=WORKER,
            json={**envelope(E1, lease_token=claim["lease_token"], kind="progress", payload={}),
                  "extensions": [SERVING_EXTENSION, JOB_EVENTS_EXTENSION]}))
        h.denied(lambda: h.client.post(BASE + f"/jobs/{claim['job_id']}/fail", headers=WORKER,
            json=envelope(E1, lease_token=claim["lease_token"], error="synthetic", retryable=True)))

    def test_fence_cleanup_then_e2_only_claim_renew_complete_and_publish(self, epoch_harness):
        h = epoch_harness
        first = h.seed_claim()
        stage = h.stage(first, E1)
        assert stage.status_code == 201
        old_ref = stage.json()
        receipt = h.assert_fenced()
        h.denied(lambda: h.register(E2))  # old attempt still needs accounting
        cancelled = h.client.post(f"/v1/jobs/{first['job_id']}/cancel", headers=CLIENT)
        assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
        assert h.register(E2).status_code == 201
        queued = h.submit()
        current = h.claim(E2)
        assert current.status_code == 200
        second = current.json()
        assert second["job_id"] == queued["job_id"]
        h.denied(lambda: h.register(E1))
        h.denied(lambda: h.claim(E1))
        h.denied(lambda: h.renew(second, E1))
        h.denied(lambda: h.complete(second, E1))
        h.denied(lambda: h.stage(second, E1))
        assert h.renew(second, E2).status_code == 200
        staged = h.stage(second, E2)
        assert staged.status_code == 201
        new_ref = staged.json()
        assert h.complete(second, E2, (new_ref,)).status_code == 200
        assert h.readback()["jobs"][second["job_id"]]["status"] == "succeeded"
        assert h.publication(second, new_ref).content == WAV
        assert h.publication(first, old_ref).status_code == 404
        assert h.read_fence().json() == receipt

    def test_offline_without_explicit_fence_retains_legacy_semantics(self, epoch_harness):
        h = epoch_harness
        claim = h.seed_claim()
        assert h.state(E1, "offline").status_code == 200
        assert h.renew(claim, E1).status_code == 200
        assert h.complete(claim, E1).status_code == 200
        assert h.register(E2).status_code == 201
        assert h.register(E1).status_code == 201
        assert h.read_fence().status_code == 404

    def test_fence_authority_is_separate_from_worker_and_client_roles(self, epoch_harness):
        h = epoch_harness
        h.seed_claim()
        for headers in ({}, WORKER, CLIENT, {"authorization": "Bearer " + AUDIO_TOKEN}):
            h.denied(lambda headers=headers: h.client.post(
                f"/v1/admin/audio-epochs/{WORKER_ID}/{E1}/fence", headers=headers,
                json=h.fence_body()), status=401)
            h.denied(lambda headers=headers: h.client.get(
                f"/v1/admin/audio-epochs/{WORKER_ID}/{E1}", headers=headers), status=401)
        assert h.read_fence().status_code == 404

    def test_fence_replay_identity_and_conflicts_are_read_back(self, epoch_harness):
        h = epoch_harness
        h.seed_claim()
        h.denied(lambda: h.fence(E2))
        receipt = h.assert_fenced()
        before = h.effects()
        assert h.fence().json() == receipt
        assert h.effects() == before
        h.denied(lambda: h.fence(request_id="42345678-1234-4234-9234-123456789abc"))
        body = {**h.fence_body(), "expected_serving_contract_revision": "sha256:" + "0" * 64}
        h.denied(lambda: h.client.post(
            f"/v1/admin/audio-epochs/{WORKER_ID}/{E1}/fence", headers=OPERATOR, json=body))
        assert h.read_fence().json() == receipt

    def test_fence_withdraws_reads_of_previously_completed_output(self, epoch_harness):
        h = epoch_harness
        claim = h.seed_claim()
        staged = h.stage(claim, E1)
        assert staged.status_code == 201
        reference = staged.json()
        assert h.complete(claim, E1, (reference,)).status_code == 200
        assert h.publication(claim, reference).status_code == 200
        h.assert_fenced()
        before = h.effects()
        assert h.publication(claim, reference).status_code == 404
        assert h.effects() == before  # logical withdrawal, not physical erasure
