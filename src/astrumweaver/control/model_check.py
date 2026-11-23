"""Validate a Hugging Face model URL and plan how it could be deployed.

This only reads public metadata from the Hugging Face API. It downloads no
weights and changes nothing. The result tells an operator:

* which GGUF files the URL selects, with exact sizes and SHA-256 digests
  pinned to a commit, so a later download can be verified;
* whether the repository is usable at all (public, GGUF, not gated);
* an estimate of which registered Workers could hold it;
* the manual steps that would deploy it.

The only network destination is ``huggingface.co``. A URL is parsed
strictly and never fetched as given, so it cannot steer a request to
another host.
"""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, unquote, urlsplit

import httpx

HF_BASE_URL = "https://huggingface.co"
_HOSTS = frozenset({"huggingface.co", "www.huggingface.co", "hf.co"})
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
_REVISION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")
_FILE_PATH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._()+=@\[\]/-]{0,299}$")
_SHARD = re.compile(r"^(?P<stem>.+)-(?P<index>\d{5})-of-(?P<total>\d{5})\.gguf$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")

MAX_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_TREE_PAGES = 5
# Rough per-Worker allowances for the KV cache, compute buffers and the OS.
VRAM_RESERVE_MB = 1536
HOST_RAM_RESERVE_MB = 4096

PASS, WARN, FAIL = "pass", "warn", "fail"


class ModelCheckError(ValueError):
    """The input is not a usable Hugging Face reference (client error)."""


class UpstreamError(RuntimeError):
    """Hugging Face is unavailable or answered unexpectedly (not the caller's fault)."""


@dataclass(frozen=True)
class HfReference:
    repo_id: str
    revision: str | None = None
    path: str | None = None


def parse_hf_reference(value: str) -> HfReference:
    """Parse ``owner/repo`` or a huggingface.co / hf.co URL strictly."""
    if not isinstance(value, str) or not value.strip():
        raise ModelCheckError("Hugging Face の URL または owner/repo を入力してください")
    text = value.strip()
    if len(text) > 500 or any(ord(ch) < 32 for ch in text):
        raise ModelCheckError("入力が長すぎるか、制御文字を含んでいます")
    if "://" in text:
        try:
            parts = urlsplit(text)
            hostname, port = parts.hostname or "", parts.port
            username, password = parts.username, parts.password
        except ValueError:  # bad port, unbalanced [ ], unicode that normalizes to a delimiter
            raise ModelCheckError("URL を解釈できません") from None
        if parts.scheme != "https":
            raise ModelCheckError("https の URL のみ使えます")
        if hostname not in _HOSTS or port not in (None, 443):
            raise ModelCheckError("huggingface.co の URL のみ使えます")
        if username or password:
            raise ModelCheckError("URL に認証情報を含めることはできません")
        segments = [s for s in parts.path.split("/") if s]
    else:
        segments = [s for s in text.split("/") if s]
    if len(segments) < 2:
        raise ModelCheckError("owner/repo の形式で入力してください")
    owner, repo, rest = segments[0], segments[1], segments[2:]
    if not _NAME.fullmatch(owner) or not _NAME.fullmatch(repo):
        raise ModelCheckError("owner またはリポジトリ名が正しくありません")
    revision: str | None = None
    path: str | None = None
    if rest:
        if rest[0] not in {"tree", "blob", "resolve"} or len(rest) < 2:
            raise ModelCheckError("/tree/<rev>、/blob/<rev>/<ファイル>、/resolve/<rev>/<ファイル> のいずれかの形式にしてください")
        revision = rest[1]
        if not _REVISION.fullmatch(revision) or ".." in revision:
            raise ModelCheckError("リビジョンが正しくありません（/ を含むブランチ名は、コミットのハッシュで指定してください）")
        if len(rest) > 2:
            if rest[0] == "tree":
                raise ModelCheckError("/tree/ の URL ではファイルを指定できません。/blob/ か /resolve/ を使ってください")
            path = "/".join(rest[2:])
            if not _FILE_PATH.fullmatch(path) or ".." in path.split("/"):
                raise ModelCheckError("ファイルのパスが正しくありません")
    return HfReference(f"{owner}/{repo}", revision, path)


def shard_group(files: Sequence[Mapping[str, Any]], selected: str) -> list[Mapping[str, Any]]:
    """The file itself, or every shard of the same split GGUF."""
    match = _SHARD.match(selected)
    by_path = {f["path"]: f for f in files}
    if match is None:
        return [by_path[selected]] if selected in by_path else []
    total = int(match["total"])
    names = [f"{match['stem']}-{i:05d}-of-{total:05d}.gguf" for i in range(1, total + 1)]
    return [by_path[n] for n in names if n in by_path]


def _check(checks: list[dict[str, str]], check_id: str, status: str, message: str) -> None:
    checks.append({"id": check_id, "status": status, "message": message})


def _count(value: Any) -> int:
    """A non-negative integer from untrusted JSON, else 0."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value) if math.isfinite(value) and value > 0 else 0


def estimate_fit(
    weights_mb: float, workers: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Rough verdict per Worker. An estimate, never a guarantee."""
    results: list[dict[str, Any]] = []
    for worker in workers:
        vram = _count(worker.get("vram_total_mb"))
        ram = _count(worker.get("ram_total_mb")) or None
        entry: dict[str, Any] = {
            "worker_id": worker.get("worker_id"),
            "backend": worker.get("backend"),
            "capabilities": list(worker.get("capabilities") or []),
            "vram_total_mb": vram,
            "ram_total_mb": ram,
        }
        if not vram:
            entry["verdict"] = "unknown"
            entry["note"] = "この Worker には GPU メモリが登録されていません"
        elif weights_mb <= vram - VRAM_RESERVE_MB:
            entry["verdict"] = "fits_gpu"
            entry["note"] = "重みが VRAM に収まり、KV キャッシュの余裕もあります"
        elif ram and weights_mb <= (vram - VRAM_RESERVE_MB) + (ram - HOST_RAM_RESERVE_MB):
            entry["verdict"] = "hybrid"
            entry["note"] = (
                "CPU/GPU の併用が必要です（MoE モデル向け）。速度は大きく落ちます"
            )
        else:
            entry["verdict"] = "too_large"
            entry["note"] = "重みが VRAM とホスト RAM の合計を超えます"
        results.append(entry)
    return results


def _plan(repo_id: str, sha: str, files: Sequence[Mapping[str, Any]], fits: Sequence[Mapping[str, Any]]) -> list[str]:
    candidates = [f["worker_id"] for f in fits if f["verdict"] in {"fits_gpu", "hybrid"}]
    steps = [
        "載せられる Worker を 1 台借りる（tsumgi-gpu-mode development）。同じプロファイルの他の Worker は処理を続けます。"
        if candidates else
        "重みを載せられる Worker がありません。より小さい量子化を選んでください。",
    ]
    for f in files:
        url = f"{HF_BASE_URL}/{repo_id}/resolve/{sha}/{quote(f['path'])}"
        steps.append(f"{url} をダウンロードし、SHA-256 {f['sha256'] or '（未公開）'} を確認する。")
    steps += [
        "実行用の設定（runtime manifest・serving contract・チャットのカタログ項目）を書く。",
        "オフラインの事前確認、Worker の起動、受け入れ試験を行ってから、リクエストを流す。",
        "カタログを更新して Control を再起動し、借りた Worker を戻す。",
    ]
    return steps


class HuggingFaceClient:
    """Minimal bounded reader of the public Hugging Face API."""

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 10.0,
        max_concurrency: int = 2,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=HF_BASE_URL,
            transport=transport,
            timeout=timeout_seconds,
            follow_redirects=False,
            headers={"user-agent": "tsumgi-admin/1", "accept": "application/json"},
        )
        self._slots = asyncio.Semaphore(max_concurrency)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_json(self, path: str, params: Mapping[str, str] | None = None) -> tuple[int, Any, Mapping[str, str]]:
        async with self._slots:
            async with self._client.stream("GET", path, params=params) as response:
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise ModelCheckError("Hugging Face の応答が大きすぎます")
                headers = dict(response.headers)
                status = response.status_code
        if status != 200:
            return status, None, headers
        try:
            return status, httpx.Response(200, content=bytes(body)).json(), headers
        except ValueError as exc:
            raise ModelCheckError("Hugging Face の応答を解釈できません") from exc


def _raise_for_status(status: int) -> None:
    """Turn an unexpected Hugging Face status into the right error class."""
    if 300 <= status < 400:
        raise ModelCheckError("リポジトリが移動または改名されています。新しい URL で試してください")
    raise UpstreamError(f"Hugging Face がエラーを返しました（ステータス {status}）")


async def _list_tree(client: HuggingFaceClient, repo_id: str, revision: str) -> tuple[list[dict[str, Any]], bool]:
    """All files of a revision (follows the API's cursor pagination).

    Returns the files and whether the list was cut at ``MAX_TREE_PAGES``.
    """
    files: list[dict[str, Any]] = []
    path = f"/api/models/{repo_id}/tree/{quote(revision, safe='')}"
    params: dict[str, str] | None = {"recursive": "true"}
    for _ in range(MAX_TREE_PAGES):
        status, data, headers = await client.get_json(path, params)
        if status != 200:
            _raise_for_status(status)
        if not isinstance(data, list):
            raise UpstreamError("Hugging Face のファイル一覧の形式が想定と違います")
        for item in data:
            if not isinstance(item, dict) or item.get("type") != "file":
                continue
            file_path = item.get("path")  # not `path`: that is the request path
            if not isinstance(file_path, str) or not file_path:
                continue
            lfs = item.get("lfs") if isinstance(item.get("lfs"), dict) else {}
            sha = lfs.get("oid")
            files.append({
                "path": file_path,
                "size_bytes": _count(lfs.get("size")) or _count(item.get("size")),
                "sha256": sha if isinstance(sha, str) and _SHA256.fullmatch(sha) else None,
            })
        next_url = re.search(r'<([^>]+)>;\s*rel="next"', headers.get("link", ""))
        cursor = re.search(r"[?&]cursor=([^&>]+)", next_url.group(1)) if next_url else None
        if cursor is None:
            return files, False
        # The cursor in the header is already percent-encoded; httpx encodes
        # query parameters again, so decode it once first.
        params = {"recursive": "true", "cursor": unquote(cursor.group(1))}
    return files, True


async def check_model(
    value: str,
    workers: Sequence[Mapping[str, Any]],
    client: HuggingFaceClient,
) -> dict[str, Any]:
    ref = parse_hf_reference(value)
    checks: list[dict[str, str]] = []
    result: dict[str, Any] = {
        "input": value.strip(),
        "repo": ref.repo_id,
        "checks": checks,
        "files": [],
        "selected": [],
        "fit": [],
        "plan": [],
    }
    revision = ref.revision or "main"
    status, info, _ = await client.get_json(f"/api/models/{ref.repo_id}", {"revision": revision})
    if status in (401, 403, 404):
        _check(checks, "reachable", FAIL, "リポジトリが見つからないか、非公開、またはアクセス申請が必要です")
        result["ok"] = False
        return result
    if status != 200:
        _raise_for_status(status)
    if not isinstance(info, dict):
        raise UpstreamError("Hugging Face の応答の形式が想定と違います")
    _check(checks, "reachable", PASS, f"{ref.repo_id} を確認しました")
    sha = info.get("sha") if isinstance(info.get("sha"), str) and _COMMIT.fullmatch(info["sha"]) else None
    result["revision"] = revision
    result["commit"] = sha
    if info.get("private") or info.get("gated"):
        _check(checks, "access", FAIL, "ゲート付き・非公開のリポジトリには対応していません")
    else:
        _check(checks, "access", PASS, "公開リポジトリです（アクセス申請は不要）")
    card = info.get("cardData") if isinstance(info.get("cardData"), dict) else {}
    license_name = card.get("license") if isinstance(card.get("license"), str) else None
    result["license"] = license_name
    _check(
        checks, "license", PASS if license_name else WARN,
        f"ライセンス: {license_name}" if license_name else "ライセンスが宣言されていません。利用前にモデルカードを確認してください",
    )
    gguf = info.get("gguf") if isinstance(info.get("gguf"), dict) else {}
    if gguf:
        result["gguf"] = {k: gguf.get(k) for k in ("architecture", "context_length", "total") if k in gguf}

    if not sha:
        _check(checks, "commit", FAIL, "コミットを特定できないため、ファイルを固定できません")
        result["ok"] = False
        return result
    tree, truncated = await _list_tree(client, ref.repo_id, sha)
    ggufs = [f for f in tree if f["path"].lower().endswith(".gguf")]
    result["files"] = ggufs
    result["truncated"] = truncated
    if truncated:
        _check(checks, "listing", WARN, f"ファイルが多いため、先頭の {MAX_TREE_PAGES} ページ分で一覧を打ち切りました。一部のファイルが見えていない可能性があります")
    if not ggufs:
        _check(checks, "format", FAIL, "GGUF ファイルがありません。現在対応しているバックエンドは GGUF のみ読み込めます")
        result["ok"] = False
        return result
    _check(checks, "format", PASS, f"GGUF ファイル {len(ggufs)} 件")

    selected: list[Mapping[str, Any]] = []
    if ref.path is not None:
        selected = shard_group(ggufs, ref.path)
        if not selected:
            _check(checks, "selection", FAIL, f"{ref.path} はこのリビジョンの GGUF ファイルではありません")
    elif len(ggufs) == 1 or len({_SHARD.sub(r"\g<stem>", f["path"]) for f in ggufs}) == 1:
        selected = shard_group(ggufs, ggufs[0]["path"])
    else:
        _check(checks, "selection", WARN, "ファイルが複数あります。量子化を選ぶには /blob/ の URL を指定してください")
    if selected:
        total_mb = sum(f["size_bytes"] for f in selected) / (1024 * 1024)
        result["selected"] = list(selected)
        result["weights_mb"] = round(total_mb)
        _check(checks, "selection", PASS, f"{len(selected)} ファイル、{total_mb / 1024:.1f} GiB")
        missing = [f["path"] for f in selected if not f["sha256"]]
        _check(
            checks, "integrity",
            WARN if missing else PASS,
            "SHA-256 が公開されていないファイル: " + ", ".join(missing) if missing else "すべてのファイルに SHA-256 があります",
        )
        shard = _SHARD.match(selected[0]["path"])
        if shard and len(selected) != int(shard["total"]):
            _check(checks, "shards", FAIL, f"分割ファイルは {int(shard['total'])} 個のはずが {len(selected)} 個しかありません")
        result["fit"] = estimate_fit(total_mb, workers)
        result["plan"] = _plan(ref.repo_id, sha, selected, result["fit"])
    result["ok"] = all(c["status"] != FAIL for c in checks) and bool(selected)
    return result
