"""#232 R17 loopback-only *disposable* InMemory Control with strict bearer auth.

It is deliberately NOT PostgreSQL production Control. Never serve with public
bind; never ingest production Worker IDs, client tokens, or catalog rows.
"""
from __future__ import annotations

import os
from typing import Any

from bonsai_12k_r17_lab_config import LAB_PROFILE, LAB_WORKER
from fastapi import FastAPI, HTTPException, Request

from astrumweaver.control import (
    InMemoryControlRepository,
    NotFoundError,
    WorkerRegistration,
)
from astrumweaver.control.api import create_app
from astrumweaver.gateway.api import create_chat_router
from astrumweaver.gateway.chat import ChatProfileCatalog


class R17OnlyRepository(InMemoryControlRepository):
    """Reject a foreign Worker ID/revision before mutating lab records."""

    def __init__(self, *, expected_deployment: str, expected_contract: str):
        super().__init__(lease_seconds=240, worker_ttl_seconds=120)
        self.expected_deployment = expected_deployment
        self.expected_contract = expected_contract

    def register_worker(
        self, registration: WorkerRegistration, *,
        recovery: bool = False, now=None,
    ):
        serving = registration.serving
        if (registration.spec.worker_id != LAB_WORKER
                or registration.spec.capabilities !=
                frozenset({"llm.chat", "text.generate"})
                or serving is None
                or serving.deployment_revision != self.expected_deployment
                or len(serving.contracts) != 1
                or serving.contracts[0].revision != self.expected_contract
                or serving.runtime_instance.deployment_revision !=
                self.expected_deployment):
            raise ValueError("R17 foreign-or-unqualified-worker-registration")
        return super().register_worker(registration, recovery=recovery, now=now)


def create_lab_app(
    catalog_data: dict[str, Any],
    *,
    client_token: str,
    worker_token: str,
) -> FastAPI:
    if (type(client_token) is not str or type(worker_token) is not str
            or len(client_token) < 24 or len(worker_token) < 24
            or client_token == worker_token):
        raise ValueError("lab-bearer-secrets-invalid")
    catalog = ChatProfileCatalog.from_dict(catalog_data)
    if tuple(x.profile_id for x in catalog.profiles) != (LAB_PROFILE,):
        raise ValueError("lab-must-advertise-only-one-opt-in-profile")
    if any(x.resolved.deployment.execution_config_sha256 ==
           x.resolved.contract.validation_evidence_sha256 for x in catalog.profiles):
        raise ValueError("lab-identity-and-evidence-must-be-distinct")
    expected = catalog.profiles[0].resolved
    repo = R17OnlyRepository(
        expected_deployment=expected.deployment.revision,
        expected_contract=expected.contract.revision,
    )
    app = create_app(
        repo, client_token=client_token, worker_token=worker_token,
        client_auth="bearer", maintenance_interval_seconds=1.0,
    )
    # In-memory lab state only; never attach a production Repository here.
    app.state.lab_repository = repo
    app.include_router(create_chat_router(
        repo, catalog,
        client_auth="bearer", client_token=client_token,
        poll_interval_seconds=0.01, max_pending_admissions=2,
    ))
    from secrets import compare_digest

    @app.get("/__lab/status")
    def lab_status(request: Request) -> dict[str, Any]:
        if (request.headers.get("authorization", "").split(" ", 1)[0].lower()
                != "bearer"
                or not compare_digest(
                    request.headers.get("authorization", "").partition(" ")[2],
                    client_token,
                )):
            raise HTTPException(status_code=401, detail="lab bearer required")
        workers = []
        try:
            known_worker = repo.get_worker(LAB_WORKER)
        except NotFoundError:
            known_worker = None
        for record in (() if known_worker is None else (known_worker,)):
            serving = record.serving
            workers.append({
                "worker_id": record.worker_id,
                "state": record.state.value,
                "active_jobs": record.active_jobs,
                "has_serving": serving is not None,
                "deployment_revision": (
                    None if serving is None else serving.deployment_revision
                ),
                "runtime_instance_epoch": (
                    None if serving is None else serving.runtime_instance.epoch
                ),
            })
        jobs = []
        for job in repo.list_jobs()[-16:]:
            jobs.append({
                "job_id": job.job_id,
                "status": job.status.value,
                "assigned_worker_id": job.assigned_worker_id,
                "claimed_deployment_revision": job.claimed_deployment_revision,
                "claimed_serving_contract_revision": (
                    job.claimed_serving_contract_revision
                ),
                "claimed_runtime_instance_epoch": (
                    job.claimed_runtime_instance_epoch
                ),
                "serving_bound": job.serving is not None,
            })
        return {
            "status": "R17_ISOLATED_INMEMORY_LAB_NOT_PRODUCTION",
            "lab_model_id": LAB_PROFILE,
            "workers": workers, "jobs": jobs,
            "production_eligible": False,
        }

    return app


def main() -> None:
    import json
    from pathlib import Path

    import uvicorn

    # Never read production Control configuration or listen on wildcard IP.
    stage = Path("/etc/astrumweaver/bonsai-serving-r17")
    catalog = json.loads((stage / "catalog12k-lab.json").read_text())
    app = create_lab_app(
        catalog, client_token=os.environ["R17_LAB_CLIENT_TOKEN"],
        worker_token=os.environ["R17_LAB_WORKER_TOKEN"],
    )
    uvicorn.run(app, host="127.0.0.1", port=19090,
                log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
