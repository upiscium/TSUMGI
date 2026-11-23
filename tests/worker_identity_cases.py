"""Shared per-worker identity scenarios; synthetic payloads only."""
from __future__ import annotations

from audio_epoch_acceptance import (
    BASE, CLIENT, E1, E2, WORKER_ID, WAV, JOB_EVENTS_EXTENSION, envelope, registration,
)
from tsumgi.transport import SERVING_EXTENSION

OWN = "synthetic-owner-worker-token-" + "a" * 32
OTHER = "synthetic-other-worker-token-" + "b" * 32
OTHER_ID = "identity-other"
WORKERS = {WORKER_ID: OWN, OTHER_ID: OTHER}


def bearer(token):
    return {"authorization": "Bearer " + token}


def worker_path(worker_id=WORKER_ID):
    return f"/v1/workers/{worker_id}"


def register(h, worker_id, epoch=E1):
    body = registration(h.profile, epoch)
    body["spec"]["worker_id"] = worker_id
    return h.client.post("/v1/workers/register", headers=bearer(WORKERS[worker_id]), json=body)


def claim(h, worker_id=WORKER_ID):
    return h.client.post(worker_path(worker_id) + "/jobs/claim",
                         headers=bearer(WORKERS[worker_id]), json=envelope(E1))


class WorkerIdentityCases:
    def test_registration_identity_precedes_repository_mutation(self, identity_harness):
        h = identity_harness
        before = h.effects()
        body = registration(h.profile, E1)
        response = h.client.post("/v1/workers/register", headers=bearer(OTHER),
                                 json=body)
        assert response.status_code == 403
        assert "worker identity mismatch" in response.text
        assert OWN not in response.text and OTHER not in response.text
        assert h.effects() == before
        recovery = registration(h.profile, E1)
        recovery["recovery"] = True
        response = h.client.post("/v1/workers/register", headers=bearer(OTHER), json=recovery)
        assert response.status_code == 403
        assert h.effects() == before
        for invalid in ("unclaimed-worker", " " + WORKER_ID, 7):
            malformed = registration(h.profile, E1)
            malformed["spec"]["worker_id"] = invalid
            response = h.client.post("/v1/workers/register", headers=bearer(OTHER),
                                     json=malformed)
            assert response.status_code == 403
            assert h.effects() == before
        assert register(h, WORKER_ID).status_code == 201
        assert register(h, OTHER_ID).status_code == 201

    def test_worker_url_binding_and_claim_positive_control(self, identity_harness):
        h = identity_harness
        assert register(h, WORKER_ID).status_code == 201
        queued = h.submit()
        before = h.effects()
        wrong = h.client.post(BASE + "/jobs/claim", headers=bearer(OTHER),
                              json=envelope(E1))
        assert wrong.status_code == 403
        assert "worker identity mismatch" in wrong.text
        assert OWN not in wrong.text and OTHER not in wrong.text
        assert h.effects() == before
        right = claim(h)
        assert right.status_code == 200, right.text
        assert right.json()["job_id"] == queued["job_id"]
        assert right.json()["assigned_worker_id"] == WORKER_ID

    def test_foreign_identity_cannot_mutate_active_claim_or_artifacts(self, identity_harness):
        h = identity_harness
        first = h.seed_claim()
        staged = h.stage(first, E1)
        assert staged.status_code == 201
        ref = staged.json()
        other_headers = bearer(OTHER)
        base = BASE + "/jobs/" + first["job_id"]
        actions = (
            lambda: h.client.post(BASE + "/heartbeat", headers=other_headers,
                json=envelope(E1, active_job_id=first["job_id"], lease_token=first["lease_token"])),
            lambda: h.client.post(BASE + "/heartbeat", headers=other_headers,
                json=envelope(E1, active_job_id=first["job_id"], lease_token=first["lease_token"],
                              state="online")),
            lambda: h.client.post(base + "/complete", headers=other_headers,
                json=envelope(E1, lease_token=first["lease_token"],
                              result={"text": None, "artifacts": []})),
            lambda: h.client.post(base + "/complete", headers=other_headers,
                json=envelope(E1, lease_token=first["lease_token"],
                              result={"text": None, "artifacts": [ref]})),
            lambda: h.client.post(base + "/fail", headers=other_headers,
                json=envelope(E1, lease_token=first["lease_token"], error="synthetic",
                              retryable=True)),
            lambda: h.client.post(base + "/events", headers=other_headers,
                json=envelope(E1, extensions=[SERVING_EXTENSION, JOB_EVENTS_EXTENSION],
                              lease_token=first["lease_token"], kind="progress", payload={})),
            lambda: h.client.post(BASE + "/state", headers=other_headers,
                                  json=envelope(E1, state="offline")),
            lambda: h.client.get(base, headers=other_headers),
            lambda: h.client.get(base + "/audio/input", headers={
                **other_headers, "x-audio-attempt": "1",
                "x-audio-lease-token": first["lease_token"], "x-audio-runtime-epoch": E1,
            }),
            lambda: h.client.post(base + "/audio/output", headers={
                **other_headers, "content-type": "audio/wav", "x-audio-attempt": "1",
                "x-audio-lease-token": first["lease_token"], "x-audio-runtime-epoch": E1,
            }, content=WAV),
        )
        for action in actions:
            response = h.denied(action, status=403)
            assert OWN not in response.text and OTHER not in response.text
        before_expiry = h.readback()["jobs"][first["job_id"]]["lease_expires_at"]
        assert h.renew(first, E1).status_code == 200
        assert h.readback()["jobs"][first["job_id"]]["lease_expires_at"] > before_expiry
        assert h.complete(first, E1, (ref,)).status_code == 200
        assert h.publication(first, ref).content == WAV

    def test_credentials_are_worker_only_and_strict(self, identity_harness):
        h = identity_harness
        owned = h.seed_claim()
        tokens = (None, "unknown-token", "unknown-valid-shape-" + "x" * 32, "epoch-synthetic-worker-token",
                  "epoch-synthetic-control-token", "epoch-synthetic-audio-token",
                  "epoch-synthetic-operator-token")
        for token in tokens:
            headers = {} if token is None else bearer(token)
            h.denied(lambda headers=headers: h.client.post(
                "/v1/workers/register", headers=headers, json=registration(h.profile, E1)),
                status=401)
            h.denied(lambda headers=headers: h.client.post(
                BASE + "/jobs/claim", headers=headers, json=envelope(E1)), status=401)
        for token in (OWN, OTHER):
            headers = bearer(token)
            h.denied(lambda headers=headers: h.client.post(
                f"/v1/admin/audio-epochs/{WORKER_ID}/{E1}/fence", headers=headers,
                json=h.fence_body()), status=401)
            h.denied(lambda headers=headers: h.client.get(
                f"/v1/admin/audio-epochs/{WORKER_ID}/{E1}", headers=headers), status=401)
            h.denied(lambda headers=headers: h.client.post(
                f"/v1/jobs/{owned['job_id']}/cancel", headers=headers), status=401)

    def test_fence_stays_authoritative_for_each_worker_identity(self, identity_harness):
        h = identity_harness
        assert h.register(E1).status_code == 201
        first = h.seed_claim()
        receipt = h.assert_fenced()
        h.denied(lambda: h.register(E1), status=409)
        h.denied(lambda: h.claim(E1), status=409)
        h.denied(lambda: h.renew(first, E1), status=409)
        h.denied(lambda: h.complete(first, E1), status=409)
        wrong = h.client.post(BASE + "/heartbeat", headers=bearer(OTHER),
            json=envelope(E1, active_job_id=first["job_id"], lease_token=first["lease_token"]))
        assert wrong.status_code == 403
        assert h.client.post(f"/v1/jobs/{first['job_id']}/cancel", headers=CLIENT).status_code == 200
        assert register(h, WORKER_ID, E2).status_code == 201
        queued = h.submit()
        current = h.claim(E2)
        assert current.status_code == 200 and current.json()["job_id"] == queued["job_id"]
        expiry = h.readback()["jobs"][queued["job_id"]]["lease_expires_at"]
        assert h.renew(current.json(), E2).status_code == 200
        assert h.readback()["jobs"][queued["job_id"]]["lease_expires_at"] > expiry
        assert h.complete(current.json(), E2).status_code == 200
        assert h.read_fence(E1).json() == receipt

    def test_correct_principal_still_needs_url_worker_ownership_and_lease(self, identity_harness):
        h = identity_harness
        first = h.seed_claim()
        assert register(h, OTHER_ID).status_code == 201
        path = worker_path(OTHER_ID)
        job = first["job_id"]
        headers = bearer(OTHER)
        body = envelope(E1, lease_token=first["lease_token"])
        h.denied(lambda: h.client.post(path + f"/jobs/{job}/complete", headers=headers,
            json={**body, "result": {"text": None, "artifacts": []}}), status=409)
        h.denied(lambda: h.client.post(path + "/heartbeat", headers=headers,
            json=envelope(E1, active_job_id=job, lease_token=first["lease_token"])), status=409)
        h.denied(lambda: h.client.post(path + f"/jobs/{job}/events", headers=headers,
            json=envelope(E1, extensions=[SERVING_EXTENSION, JOB_EVENTS_EXTENSION],
                          lease_token=first["lease_token"], kind="progress", payload={})), status=409)
        h.denied(lambda: h.client.post(path + f"/jobs/{job}/fail", headers=headers,
            json={**body, "error": "synthetic", "retryable": True}), status=409)
        h.denied(lambda: h.client.get(path + f"/jobs/{job}", headers=headers), status=404)
        expiry = h.readback()["jobs"][job]["lease_expires_at"]
        assert h.renew(first, E1).status_code == 200
        assert h.readback()["jobs"][job]["lease_expires_at"] > expiry

    def test_all_worker_routes_have_the_identity_dependency(self, identity_harness):
        routes = [route for route in identity_harness.client.app.routes
                  if getattr(route, "path", "").startswith("/v1/workers/")]
        assert len(routes) == 10
        for route in routes:
            assert any(dependency.call.__name__ == "require_worker"
                       for dependency in route.dependant.dependencies), route.path
