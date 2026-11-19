# Operator console

A small read-only web console served by Control. It shows what each Worker
is running and checks a Hugging Face model URL before anyone deploys it.

It never changes a Worker, a Job or a deployment. Swapping a model stays a
manual, reviewed operation.

## Security: password (HTTP Basic)

With `username` and `password_hash` set (below), every `/admin` route asks for
HTTP Basic credentials, so the console can sit behind a public HTTPS proxy.
Without them **there is no login**: anyone who can reach `/admin` can read every
Worker's state and ask the Hugging Face API about a model, so treat it like an
internal dashboard:

- It is **off** unless the Control config enables it.
- Without a password, do not expose `/admin` on a public listener: route only
  `/v1/` to the outside. With one, expose it only over HTTPS.
- Optional `allowed_hosts` (below) rejects requests whose `Host` header is not
  on the list. That blocks a DNS-rebinding page, which could otherwise read
  the console through an operator's browser.
- The page sends a strict `Content-Security-Policy` (scripts and styles from
  Control itself only), and everything it shows is inserted as text, never as
  HTML.
- `POST /admin/api/model-check` requires `Content-Type: application/json`, so
  a page on another origin cannot trigger it from an operator's browser
  without a CORS preflight.

## Enabling it

In the Control config:

```toml
[admin_ui]
enabled = true
```

Then open `http://<control>:9000/admin`. The setting must be a TOML boolean.

```toml
[admin_ui]
enabled = true
allowed_hosts = ["control.example.lan", "10.12.2.13"]   # optional
```

`allowed_hosts` lists the host names (without port) the console may be opened
under. Leave it out to accept any `Host` header.

## Password

Make a hash (at least 12 characters; the password itself is never stored):

```sh
python -m astrumweaver.control.admin_auth
```

Put the printed line in the Control config:

```toml
[admin_ui]
enabled = true
username = "ops"
password_hash = "scrypt$16384$8$1$...$..."
```

Both keys are required together, and Control refuses to start with only one or
with an unusable hash. After 5 wrong attempts within 5 minutes a client gets
429 until the window passes (the nearest proxy's forwarded address identifies
the client). Failures are logged. Use it only over HTTPS: Basic credentials
are not encrypted. Pair it with `allowed_hosts` when exposing the console.

## Worker status

One card per registered Worker, refreshed every 5 seconds:

| Field | Source |
| --- | --- |
| state, active jobs, capabilities, last seen | the Worker record in Control |
| model, backend, context size | the Worker's runtime summary (heartbeat telemetry) |
| profile name | the catalog entry whose deployment revision the Worker advertises |
| GPU name and total VRAM | the registration |
| VRAM used, GPU utilization, temperature | heartbeat telemetry (`nvidia-smi`) |
| RAM, CPU count, free disk | heartbeat telemetry (the container as the Worker sees it) |

### Worker telemetry

Workers attach a snapshot to their heartbeats as `metadata["telemetry"]`
(schema `worker-telemetry-v1`). A background task refreshes it every 30 s, so
a slow `nvidia-smi` never delays a heartbeat, and a collection failure is
recorded in the snapshot (`errors`) instead of stopping the Worker.

```toml
[worker]
telemetry_interval_seconds = 30   # 0 turns telemetry off
```

Inside an LXC container, memory and CPU count describe the container, not the
Proxmox host. The load average is collected but not shown: on Proxmox three
containers on one node reported identical values, so it does not tell
containers apart. A snapshot older than 120 s is shown as stale. A Worker that
predates telemetry still appears, with the live values marked as unknown.

## Model check

Enter a Hugging Face model page URL, a `/blob/<rev>/<file>.gguf` URL, or
`owner/repo`. Control reads only public metadata from `huggingface.co` and
reports:

- whether the repository is reachable, public (not gated), and licensed;
- the GGUF files, their sizes, and SHA-256 digests, pinned to the commit the
  revision resolved to;
- an estimate, per Worker, of whether the weights fit in VRAM, need CPU/GPU
  hybrid offload, or are too large. This is a rough guide, not a guarantee;
  the real test is loading the model on a Worker;
- the manual steps that would deploy it.

Limits, on purpose:

- Only `https://huggingface.co` (or `hf.co`) is contacted. The URL is parsed
  strictly and never fetched as given.
- Only GGUF is supported, because the current backends load GGUF.
- Private and gated repositories are rejected.
- No weights are downloaded and nothing is deployed.
- Responses above 5 MiB are refused, at most two Hugging Face requests run at
  once, and one check gives up after 30 s. Request bodies above 4 KiB are
  refused.
- A listing is cut after 5 pages of files and the result says so.
- A URL that names a branch containing `/` is ambiguous (`/blob/feature/x/m.gguf`),
  so give the commit hash as the revision instead.
- Hugging Face errors are reported as 502 (429 and 5xx) or 400 (a renamed
  repository answers with a redirect, which is not followed).

## API

| Route | Purpose |
| --- | --- |
| `GET /admin` | the page (`/admin/app.js`, `/admin/app.css` are its assets) |
| `GET /admin/api/workers` | every Worker's status as JSON |
| `POST /admin/api/model-check` | body `{"url": "..."}`; returns the check result |
