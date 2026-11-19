from __future__ import annotations

import json

import httpx
import pytest
from fastapi import FastAPI

from astrumweaver import AcceleratorDevice, ResourceShape, WorkerSpec
from astrumweaver.control import InMemoryControlRepository, WorkerRegistration
from astrumweaver.control.admin import create_admin_router, fit_inputs, worker_status
from astrumweaver.control.models import utc_now
from astrumweaver.control.model_check import (
    HuggingFaceClient,
    ModelCheckError,
    UpstreamError,
    check_model,
    estimate_fit,
    parse_hf_reference,
    shard_group,
)
from astrumweaver.transport import TELEMETRY_METADATA_KEY

GPU = "GPU-f78bcab4-245f-b3ff-5570-ed629b239331"


def register(repo, worker_id="spica", *, telemetry=None, gpus=True, gpu=GPU):
    resources = ResourceShape(gpu_count=1, total_vram_mb=11264, max_single_gpu_vram_mb=11264) if gpus else ResourceShape()
    spec = WorkerSpec(
        worker_id=worker_id,
        worker_class="gpu-single",
        resources=resources,
        gpu_uuids=(gpu,) if gpus else (),
        accelerators=(AcceleratorDevice(gpu, 11264, "6.1", "NVIDIA GeForce GTX 1080 Ti"),) if gpus else (),
        capabilities=frozenset({"llm.chat"}),
    )
    metadata = {} if telemetry is None else {TELEMETRY_METADATA_KEY: telemetry}
    repo.register_worker(WorkerRegistration(spec=spec, metadata=metadata))


def snapshot(age_seconds=1.0):
    from datetime import timedelta

    return {
        "schema": "worker-telemetry-v1",
        "collected_at": (utc_now() - timedelta(seconds=age_seconds)).isoformat(),
        "runtime": {"provider_id": "strata", "model": "Q.gguf", "context_tokens": 65536},
        "gpus": [{"uuid": GPU, "memory_used_mb": 10741.0, "utilization_percent": 12.0, "temperature_c": 44.0}],
        "host": {"cpu_count": 8, "memory_total_mb": 32768, "memory_available_mb": 8192, "disk_free_mb": 50000},
    }


def test_worker_status_combines_registration_and_live_telemetry():
    repo = InMemoryControlRepository()
    register(repo, telemetry=snapshot())
    status = worker_status(repo.get_worker("spica"), utc_now(), None)
    assert status["backend"] == "strata" and status["model"] == "Q.gguf"
    assert status["context_tokens"] == 65536
    assert status["gpus"][0]["memory_total_mb"] == 11264 and status["gpus"][0]["memory_used_mb"] == 10741.0
    assert status["host"]["memory_total_mb"] == 32768
    assert status["telemetry_stale"] is False


def test_worker_status_without_telemetry_keeps_registration_facts():
    repo = InMemoryControlRepository()
    register(repo)
    status = worker_status(repo.get_worker("spica"), utc_now(), None)
    assert status["backend"] is None and status["host"] is None
    assert status["gpus"][0]["memory_used_mb"] is None and status["gpus"][0]["name"].endswith("1080 Ti")
    assert status["telemetry_stale"] is True


def test_old_telemetry_is_marked_stale():
    repo = InMemoryControlRepository()
    register(repo, telemetry=snapshot(age_seconds=600))
    assert worker_status(repo.get_worker("spica"), utc_now(), None)["telemetry_stale"] is True


def test_malformed_telemetry_never_breaks_status():
    repo = InMemoryControlRepository()
    register(repo, telemetry={"gpus": "nope", "runtime": 5, "host": [1], "collected_at": 7})
    status = worker_status(repo.get_worker("spica"), utc_now(), None)
    assert status["backend"] is None and status["host"] is None


def app_for(repo, hf_handler=None):
    app = FastAPI()
    client = HuggingFaceClient(transport=httpx.MockTransport(hf_handler or (lambda r: httpx.Response(404))))
    app.include_router(create_admin_router(repo, hf_client=client))
    return app


async def call(app, method, path, **kwargs):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://control") as client:
        return await client.request(method, path, **kwargs)


@pytest.mark.asyncio
async def test_workers_api_and_static_pages():
    repo = InMemoryControlRepository()
    register(repo, telemetry=snapshot())
    register(repo, "altair", gpus=False)
    app = app_for(repo)
    workers = await call(app, "GET", "/admin/api/workers")
    assert workers.status_code == 200 and workers.headers["cache-control"] == "no-store"
    assert [w["worker_id"] for w in workers.json()["workers"]] == ["altair", "spica"]
    for path, kind in (("/admin", "text/html"), ("/admin/", "text/html"), ("/admin/app.js", "text/javascript"), ("/admin/app.css", "text/css")):
        page = await call(app, "GET", path)
        assert page.status_code == 200 and page.headers["content-type"].startswith(kind), path
        assert "script-src 'self'" in page.headers["content-security-policy"]
        assert page.headers["x-content-type-options"] == "nosniff"


@pytest.mark.asyncio
async def test_pages_have_no_inline_script_or_remote_assets():
    app = app_for(InMemoryControlRepository())
    html = (await call(app, "GET", "/admin")).text
    assert "<script src=\"/admin/app.js\">" in html and "onclick" not in html
    assert "http://" not in html and "https://" not in html.replace("https://huggingface.co/unsloth", "")
    script = (await call(app, "GET", "/admin/app.js")).text
    assert "innerHTML" not in script and "insertAdjacentHTML" not in script and "eval(" not in script


@pytest.mark.parametrize(
    "value",
    [
        "http://huggingface.co/a/b",
        "https://evil.example/a/b",
        "https://huggingface.co.evil.example/a/b",
        "https://user:pw@huggingface.co/a/b",
        "https://huggingface.co:8443/a/b",
        "https://huggingface.co/a",
        "https://huggingface.co/a/b/blob/main/../../etc/passwd",
        "https://huggingface.co/a/b/tree/main/sub",
        "https://huggingface.co/a/b/raw/main/x.gguf",
        "a/b\nc",
        "../b",
        "",
    ],
)
def test_parse_rejects_unsafe_or_unsupported_references(value):
    with pytest.raises(ModelCheckError):
        parse_hf_reference(value)


@pytest.mark.parametrize(
    "value, expected",
    [
        ("unsloth/Q-GGUF", ("unsloth/Q-GGUF", None, None)),
        ("https://huggingface.co/unsloth/Q-GGUF", ("unsloth/Q-GGUF", None, None)),
        ("https://hf.co/unsloth/Q-GGUF/tree/main", ("unsloth/Q-GGUF", "main", None)),
        ("https://huggingface.co/unsloth/Q-GGUF/blob/abc123/dir/Q.gguf", ("unsloth/Q-GGUF", "abc123", "dir/Q.gguf")),
        ("https://huggingface.co/unsloth/Q-GGUF/resolve/main/Q-00001-of-00002.gguf", ("unsloth/Q-GGUF", "main", "Q-00001-of-00002.gguf")),
    ],
)
def test_parse_accepts_model_pages_and_files(value, expected):
    ref = parse_hf_reference(value)
    assert (ref.repo_id, ref.revision, ref.path) == expected


def test_shard_group_collects_every_part():
    files = [{"path": f"Q-{i:05d}-of-00003.gguf"} for i in (1, 2, 3)] + [{"path": "Other.gguf"}]
    assert [f["path"] for f in shard_group(files, "Q-00002-of-00003.gguf")] == [f"Q-{i:05d}-of-00003.gguf" for i in (1, 2, 3)]
    assert [f["path"] for f in shard_group(files, "Other.gguf")] == ["Other.gguf"]
    assert shard_group(files, "missing.gguf") == []


def test_estimate_fit_tiers():
    workers = [
        {"worker_id": "gpu", "backend": "strata", "vram_total_mb": 11264, "ram_total_mb": 32768},
        {"worker_id": "nogpu", "backend": None, "capabilities": ["text.embed"], "vram_total_mb": 0, "ram_total_mb": 8192},
    ]
    assert [f["verdict"] for f in estimate_fit(8000, workers)] == ["fits_gpu", "unknown"]
    assert estimate_fit(21794, workers)[0]["verdict"] == "hybrid"
    assert estimate_fit(60000, workers)[0]["verdict"] == "too_large"


def hf_handler(*, info=None, tree=None, status=200, tree_status=200):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "huggingface.co"
        path = request.url.path
        if "/tree/" in path:
            return httpx.Response(tree_status, json=tree or [])
        return httpx.Response(status, json=info or {})

    return handler


def lfs(path, size, sha="a" * 64):
    return {"type": "file", "path": path, "size": 134, "lfs": {"oid": sha, "size": size}}


COMMIT = "c0ffee" * 6 + "c0ff"  # a real commit id is 40 hex digits
INFO = {"sha": COMMIT, "private": False, "gated": False, "cardData": {"license": "apache-2.0"},
        "gguf": {"architecture": "qwen35moe", "context_length": 262144}}


@pytest.mark.asyncio
async def test_check_model_single_quant_passes_with_plan_and_fit():
    tree = [lfs("README.md", 0), lfs("Q4.gguf", 21794 * 1024 * 1024)]
    tree[0] = {"type": "file", "path": "README.md", "size": 10}
    client = HuggingFaceClient(transport=httpx.MockTransport(hf_handler(info=INFO, tree=tree)))
    workers = [{"worker_id": "spica", "backend": "strata", "vram_total_mb": 11264, "ram_total_mb": 32768}]
    result = await check_model("unsloth/Q-GGUF", workers, client)
    assert result["ok"] is True and result["commit"] == COMMIT and result["license"] == "apache-2.0"
    assert result["gguf"]["architecture"] == "qwen35moe"
    assert [f["path"] for f in result["selected"]] == ["Q4.gguf"] and result["weights_mb"] == 21794
    assert result["fit"][0]["verdict"] == "hybrid"
    assert any(f"resolve/{COMMIT}/Q4.gguf" in step and "a" * 64 in step for step in result["plan"])
    assert {c["status"] for c in result["checks"]} == {"pass"}


@pytest.mark.asyncio
async def test_check_model_asks_for_a_choice_when_several_quantizations():
    tree = [lfs("A-Q4.gguf", 100), lfs("A-Q8.gguf", 200)]
    client = HuggingFaceClient(transport=httpx.MockTransport(hf_handler(info=INFO, tree=tree)))
    result = await check_model("o/r", [], client)
    assert result["ok"] is False and result["selected"] == []
    assert any(c["id"] == "selection" and c["status"] == "warn" for c in result["checks"])
    picked = await check_model("https://huggingface.co/o/r/blob/main/A-Q8.gguf", [], client)
    assert picked["ok"] is True and picked["weights_mb"] == 0 and picked["selected"][0]["path"] == "A-Q8.gguf"


@pytest.mark.asyncio
async def test_check_model_rejects_gated_non_gguf_missing_and_incomplete_shards():
    gated = HuggingFaceClient(transport=httpx.MockTransport(hf_handler(info={**INFO, "gated": "manual"}, tree=[lfs("Q.gguf", 1)])))
    assert (await check_model("o/r", [], gated))["ok"] is False

    nongguf = HuggingFaceClient(transport=httpx.MockTransport(hf_handler(info=INFO, tree=[lfs("m.safetensors", 1)])))
    result = await check_model("o/r", [], nongguf)
    assert result["ok"] is False and any(c["id"] == "format" and c["status"] == "fail" for c in result["checks"])

    missing = HuggingFaceClient(transport=httpx.MockTransport(hf_handler(status=404)))
    assert (await check_model("o/r", [], missing))["checks"][0]["status"] == "fail"

    partial = HuggingFaceClient(transport=httpx.MockTransport(hf_handler(info=INFO, tree=[lfs("Q-00001-of-00003.gguf", 1), lfs("Q-00003-of-00003.gguf", 1)])))
    result = await check_model("https://huggingface.co/o/r/blob/main/Q-00001-of-00003.gguf", [], partial)
    assert result["ok"] is False and any(c["id"] == "shards" for c in result["checks"])


@pytest.mark.asyncio
async def test_check_model_warns_without_license_or_sha256():
    info = {"sha": COMMIT, "private": False, "gated": False}
    tree = [{"type": "file", "path": "Q.gguf", "size": 5}]
    client = HuggingFaceClient(transport=httpx.MockTransport(hf_handler(info=info, tree=tree)))
    result = await check_model("o/r", [], client)
    statuses = {c["id"]: c["status"] for c in result["checks"]}
    assert statuses["license"] == "warn" and statuses["integrity"] == "warn" and result["ok"] is True


@pytest.mark.asyncio
async def test_oversized_hf_response_is_refused():
    big = HuggingFaceClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"x" * (6 * 1024 * 1024))))
    with pytest.raises(ModelCheckError, match="大きすぎ"):
        await check_model("o/r", [], big)


@pytest.mark.asyncio
async def test_model_check_endpoint_validation_and_success():
    repo = InMemoryControlRepository()
    register(repo, telemetry=snapshot())
    app = app_for(repo, hf_handler(info=INFO, tree=[lfs("Q4.gguf", 1024 * 1024 * 1024)]))
    assert (await call(app, "POST", "/admin/api/model-check", content=b'{"url":"o/r"}')).status_code == 415
    assert (await call(app, "POST", "/admin/api/model-check", headers={"content-type": "application/json"}, content=b"nope")).status_code == 400
    bad = await call(app, "POST", "/admin/api/model-check", json={"url": "https://evil.example/o/r"})
    assert bad.status_code == 400 and "huggingface.co" in bad.json()["error"]
    assert (await call(app, "POST", "/admin/api/model-check", json={"url": 5})).status_code == 400
    ok = await call(app, "POST", "/admin/api/model-check", json={"url": "o/r"})
    assert ok.status_code == 200 and ok.json()["ok"] is True
    assert ok.json()["fit"][0]["worker_id"] == "spica" and ok.json()["fit"][0]["ram_total_mb"] == 32768


@pytest.mark.asyncio
async def test_unreachable_hugging_face_is_a_bad_gateway():
    def boom(request):
        raise httpx.ConnectError("down")

    app = app_for(InMemoryControlRepository(), boom)
    response = await call(app, "POST", "/admin/api/model-check", json={"url": "o/r"})
    assert response.status_code == 502 and "Hugging Face" in response.json()["error"]


def test_fit_inputs_skip_offline_and_gpu_less_workers():
    repo = InMemoryControlRepository()
    register(repo, "a", telemetry=snapshot())
    register(repo, "b", gpus=False)
    now = utc_now()
    statuses = [worker_status(repo.get_worker(w), now, None) for w in ("a", "b")]
    assert [i["worker_id"] for i in fit_inputs(statuses)] == ["a"]
    statuses[0]["state"] = "offline"
    assert fit_inputs(statuses) == []


@pytest.mark.parametrize(
    "value",
    ["https://huggingface.co:abc/a/b", "https://[abc/x/y", "https://huggingface.co\u2100/a/b", "https://huggingface.co:99999999/a/b"],
)
def test_malformed_urls_are_client_errors_not_crashes(value):
    with pytest.raises(ModelCheckError):
        parse_hf_reference(value)


@pytest.mark.asyncio
async def test_malformed_url_is_a_400_through_the_route():
    app = app_for(InMemoryControlRepository())
    response = await call(app, "POST", "/admin/api/model-check", json={"url": "https://[abc/x/y"})
    assert response.status_code == 400 and "URL" in response.json()["error"]


def _status_for(telemetry):
    repo = InMemoryControlRepository()
    register(repo, telemetry=telemetry)
    return worker_status(repo.get_worker("spica"), utc_now(), None)


@pytest.mark.parametrize(
    "telemetry",
    [
        {"gpus": None},
        {"gpus": [{"uuid": ["x"]}, "nope", None, {"uuid": GPU, "memory_used_mb": "lots"}]},
        {"host": {"load_1m": "x", "memory_total_mb": "32768", "cpu_count": True, "disk_free_mb": float("inf")}},
        {"host": {"load_1m": float("nan")}},
        {"errors": "boom"},
        {"errors": [1, None, ["x"], "ok"]},
        {"runtime": {"provider_id": 5, "model": ["m"], "context_tokens": "65536"}},
        {"runtime": {"context_tokens": float("nan")}},
        {"collected_at": "not a time"},
        {"host": [1, 2], "gpus": {"a": 1}, "runtime": "x"},
    ],
)
def test_worker_status_normalizes_hostile_telemetry(telemetry):
    status = _status_for(telemetry)
    # It must be strict JSON (no NaN/inf) and every numeric field a real number or None.
    json.dumps(status, allow_nan=False)
    for key in ("memory_used_mb", "utilization_percent", "temperature_c", "power_w"):
        assert status["gpus"][0][key] is None or isinstance(status["gpus"][0][key], (int, float))
    if status["host"] is not None:
        assert all(v is None or (isinstance(v, (int, float)) and not isinstance(v, bool)) for v in status["host"].values())
    assert all(isinstance(e, str) for e in status["telemetry_errors"])
    assert status["backend"] is None or isinstance(status["backend"], str)


def test_worker_status_keeps_only_known_host_fields_and_short_text():
    status = _status_for({"host": {"cpu_count": 8, "secret": "x" * 10}, "errors": ["e" * 1000] * 20,
                          "runtime": {"provider_id": "p" * 500, "model": "m" * 500}})
    assert set(status["host"]) == {"cpu_count", "load_1m", "load_5m", "memory_total_mb", "memory_available_mb", "disk_total_mb", "disk_free_mb"}
    assert len(status["telemetry_errors"]) == 5 and all(len(e) == 200 for e in status["telemetry_errors"])
    assert len(status["backend"]) == 64 and len(status["model"]) == 200


@pytest.mark.asyncio
async def test_one_hostile_worker_does_not_break_the_list_or_the_model_check():
    repo = InMemoryControlRepository()
    register(repo, "good", telemetry=snapshot())
    register(repo, "bad", gpu="GPU-bad-0000", telemetry={"gpus": None, "host": {"memory_total_mb": "x", "load_1m": float("nan")}, "errors": "boom"})
    app = app_for(repo, hf_handler(info=INFO, tree=[lfs("Q4.gguf", 1024 * 1024 * 1024)]))
    listing = await call(app, "GET", "/admin/api/workers")
    assert listing.status_code == 200 and [w["worker_id"] for w in listing.json()["workers"]] == ["bad", "good"]
    check = await call(app, "POST", "/admin/api/model-check", json={"url": "o/r"})
    assert check.status_code == 200 and {f["worker_id"] for f in check.json()["fit"]} == {"good", "bad"}


@pytest.mark.asyncio
@pytest.mark.parametrize("status, expected", [(429, 502), (500, 502), (503, 502), (301, 400), (302, 400)])
async def test_upstream_statuses_map_to_the_right_error(status, expected):
    app = app_for(InMemoryControlRepository(), hf_handler(status=status))
    response = await call(app, "POST", "/admin/api/model-check", json={"url": "o/r"})
    assert response.status_code == expected
    if status in (301, 302):
        assert "改名" in response.json()["error"]


@pytest.mark.asyncio
async def test_tree_listing_failure_is_an_upstream_error():
    client = HuggingFaceClient(transport=httpx.MockTransport(hf_handler(info=INFO, tree_status=500)))
    with pytest.raises(UpstreamError):
        await check_model("o/r", [], client)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_size", ["abc", {"a": 1}, [1], None, -5, float("nan"), True])
async def test_odd_file_sizes_from_hugging_face_are_ignored(bad_size):
    tree = [{"type": "file", "path": "Q.gguf", "size": bad_size, "lfs": {"oid": "a" * 64, "size": bad_size}},
            {"type": "file", "size": 5}, {"type": "file", "path": 7}, "junk", None]

    def handler(request: httpx.Request) -> httpx.Response:
        if "/tree/" in request.url.path:
            # json.dumps writes NaN as a bare literal, which Python's parser still accepts.
            return httpx.Response(200, content=json.dumps(tree), headers={"content-type": "application/json"})
        return httpx.Response(200, json=INFO)

    client = HuggingFaceClient(transport=httpx.MockTransport(handler))
    result = await check_model("o/r", [], client)
    assert [f["path"] for f in result["files"]] == ["Q.gguf"] and result["files"][0]["size_bytes"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("sha", ["../../x", "abc", "c0ffee", "G" * 40, None, 5])
async def test_a_commit_that_is_not_40_hex_digits_is_refused(sha):
    client = HuggingFaceClient(transport=httpx.MockTransport(hf_handler(info={**INFO, "sha": sha}, tree=[lfs("Q.gguf", 1)])))
    result = await check_model("o/r", [], client)
    assert result["ok"] is False and any(c["id"] == "commit" and c["status"] == "fail" for c in result["checks"])
    assert result["plan"] == []


@pytest.mark.asyncio
async def test_pagination_decodes_the_link_cursor_once_and_follows_pages():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "/tree/" not in request.url.path:
            return httpx.Response(200, json=INFO)
        cursor = request.url.params.get("cursor")
        seen.append(cursor)
        if cursor is None:
            link = '<https://huggingface.co/api/models/o/r/tree/x?cursor=ab%3D%3D&recursive=true>; rel="next"'
            return httpx.Response(200, json=[lfs("A.gguf", 1)], headers={"link": link})
        return httpx.Response(200, json=[lfs("B.gguf", 2)])

    client = HuggingFaceClient(transport=httpx.MockTransport(handler))
    result = await check_model("o/r", [], client)
    assert seen == [None, "ab=="]  # decoded once, not "ab%3D%3D" and not double-encoded
    assert [f["path"] for f in result["files"]] == ["A.gguf", "B.gguf"] and result["truncated"] is False


@pytest.mark.asyncio
async def test_a_truncated_listing_is_reported_not_silent():
    def handler(request: httpx.Request) -> httpx.Response:
        if "/tree/" not in request.url.path:
            return httpx.Response(200, json=INFO)
        link = '<https://huggingface.co/api/models/o/r/tree/x?cursor=next&recursive=true>; rel="next"'
        return httpx.Response(200, json=[lfs("A.gguf", 1)], headers={"link": link})

    client = HuggingFaceClient(transport=httpx.MockTransport(handler))
    result = await check_model("o/r", [], client)
    assert result["truncated"] is True
    assert any(c["id"] == "listing" and c["status"] == "warn" for c in result["checks"])


def test_fit_estimate_ignores_non_numeric_worker_facts():
    fits = estimate_fit(8000, [{"worker_id": "w", "vram_total_mb": "11264", "ram_total_mb": "lots"},
                               {"worker_id": "x", "vram_total_mb": float("nan"), "ram_total_mb": -1}])
    assert [f["verdict"] for f in fits] == ["unknown", "unknown"]


@pytest.mark.asyncio
async def test_oversized_request_body_is_refused():
    app = app_for(InMemoryControlRepository())
    big = json.dumps({"url": "o/r", "pad": "x" * 10000})
    response = await call(app, "POST", "/admin/api/model-check", headers={"content-type": "application/json"}, content=big)
    assert response.status_code == 413


@pytest.mark.asyncio
async def test_allowed_hosts_blocks_other_host_headers():
    repo = InMemoryControlRepository()
    app = FastAPI()
    app.include_router(create_admin_router(repo, allowed_hosts=["control.lan", "10.12.2.13", "::1"]))

    async def get(host):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://x") as client:
            return await client.get("/admin/api/workers", headers={"host": host})

    for ok in ("control.lan", "CONTROL.LAN:9000", "10.12.2.13:9000", "[::1]:9000"):
        assert (await get(ok)).status_code == 200, ok
    for bad in ("evil.example", "evil.example:9000", "control.lan.evil.example", ""):
        assert (await get(bad)).status_code == 403, bad
    open_app = app_for(repo)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=open_app), base_url="http://x") as client:
        assert (await client.get("/admin/api/workers", headers={"host": "anything"})).status_code == 200
