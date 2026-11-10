# TSUMGI #232 — R20 isolated Spica12K real TCP Cancellation runbook

**Scope: one bounded Spica-only lab trial, mandatory original8K restore.**
Not production Serving, PostgreSQL Control, published client model or
independent host/physical attestation. Keep Issue #232 OPEN and PR #250 Draft.

## Preflight baseline and historical outcomes

R19 physically passed four isolated Gateway stages, five actual Managed
Worker Job Claims and original8K restoration, but **did not test
Cancellation**. R17 and R18 failed attempts and their receipts remain
unaltered. Before any R20 action, read-only check shared Control: Spica
and Deneb ONLINE, active_jobs=0 and zero nonterminal Jobs. Confirm original
Spica native context 8192 and worker READY, original8K manifests/model and
backup roots byte-pinned, GPU UUID
`GPU-f78bcab4-245f-b3ff-5570-ed629b239331`, >=3000MiB idle
free VRAM. No R20 service/timer/port may already exist.

## Deterministic reviewed source installation

The source tool `tools/bonsai_12k_r20_operator_patch.py`
takes only the exact reviewed original
`tools/bonsai_12k_r17_operator.py` (SHA256
`1b3e21be8136844a75cfcec5cb875dc205710855e0db42a80c2bf1496cb02261`)
and pinned `tools/bonsai_12k_r20_cancel_gateway.py`.
Generate the new operator OFFLINE, compile it, verify generated SHA256
against the reviewed preview, and install as root mode0600 at
`/root/tsumgi-r20-operator.py`. Source generation performs
no Worker/GPU/Control actions and must preserve prior R17/R19 evidence.

Prepare a **new** root-owned stage directory
`/etc/astrumweaver/bonsai-serving-r20/` with a copied pinned
R19 isolated lab Runtime12K, Serving, single-entry Catalog and lab Worker
config, changing only isolated stage path references. Include the exact
pinned cancellation helper source mode0640, owner root, group
astrumweaver-config. Generate fresh **distinct 256-bit secrets** into
separate private control.env and worker.env; never place secrets in
process argv, print them, or upload to GitHub.

Lab Control/Gateway is exclusively localhost 127.0.0.1:19090,
Native localhost 127.0.0.1:18085 and experimental Worker health
localhost 127.0.0.1:19100. Use Worker ID
`bonsai-spica-r17-lab` and only lab model
`bonsai2-27b-12k-lab`. No production Control DSN,
public listener, Cloudflare Tunnel, NPM or Deneb changes.

## Exact one-off systemd contract and acceptance order

Run on authorized **Spica root** only. First run
`/usr/bin/python3 /root/tsumgi-r20-operator.py --prepare`
while old8K remains READY. It records all staged files' digests
including the helper and root operator code. No old files overwritten.

Start a **new** dedicated Control transient via:
`systemd-run --unit=tsumgi-bonsai12k-spica-r20-control --uid=astrumweaver --gid=astrumweaver --property=SupplementaryGroups=astrumweaver-config --property=EnvironmentFile=/etc/astrumweaver/bonsai-serving-r20/control.env --property=Restart=no --property=RuntimeMaxSec=20min --property=KillMode=control-group --property=NoNewPrivileges=yes --property=PrivateTmp=yes --property=ProtectHome=yes --property=ProtectSystem=strict --property=RestrictAddressFamilies='AF_UNIX AF_INET AF_INET6' /nix/var/nix/profiles/tsumgi-v012-ga-installer/bin/python3 /etc/astrumweaver/bonsai-serving-r20/bonsai_12k_r17_lab_control.py`

With that local-only Control READY and its Job/Worker store empty, run
`/usr/bin/python3 /root/tsumgi-r20-operator.py --preflight`.
It must exit0 and report >=1000 seconds actual remaining Control
runtime from a finite exact 20min systemd cap, verify original8K,
operator/staged-byte provenance, actual Control PID/argv/cgroup/port
ownership, auth and lab-only profile. **Do not stop old8K if any
preflight gate fails.**

Start one bounded Controller using:
`systemd-run --unit=tsumgi-bonsai12k-spica-r20-operator --property=Restart=no --property=KillMode=control-group --property=RuntimeMaxSec=850s --setenv=TSUMGI_R20_OPERATOR_APPROVED=SPICA_R20_ISOLATED_REAL_TCP_CANCEL_8K_RESTORE /usr/bin/python3 /root/tsumgi-r20-operator.py --execute`.
An initial systemd-run success indicates only launch, not acceptance.
Require its final MainPID exit0 + reviewed terminal receipt.

**Mandatory recovery:** The Controller arms and checks a separate
`tsumgi-bonsai12k-spica-r20-recovery.timer` with
`--on-active=780s` BEFORE stopping
`astrumweaver-worker.service`. Then old Native/cgroup/GPU
ownership must be fully released; it starts only
`tsumgi-bonsai12k-spica-r20-worker.service`. A stage-wide
SIGALRM <=260s bounds cancellation. Foreground execution calls
`recover()` from its `finally` on every failure;
if it disappears, the timer invokes the same root-pinned
`--recover` script. Do not clear the watchdog/rollback
markers prematurely. If necessary after a failed operator, only
verified authorized root invokes
`/usr/bin/python3 /root/tsumgi-r20-operator.py --recover`.

## Required real TCP E2E evidence

The fixed child Python client uses urllib and receives the bearer and
serialized *actual sender HTTP JSON body bytes* through private stdin.
Neither body nor token are logged. It reports only HTTP status 200 and
its exact sender SHA256; the operator stores the raw sender bytes under
private root-owned
`/root/tsumgi-bonsai12k-serving-r20-run/actual-sender-http-body.bin`.
This is host-local sender **body** evidence; HTTP authorization and
content-type headers, transport framing and wire bytes are not captured.
It is **not** independently attested packet capture.

Require one lab Job RUNNING and assigned to the correct lab Worker
with staged Deployment/Serving/Runtime epoch claims, Worker active_jobs=1,
Native slot n_ctx12288 **is_processing=true** before intentional
SIGKILL of the client subprocess/TCP connection. Do **not** call the
Control Cancellation endpoint as a shortcut.

After client death, require the same Job durably CANCELLED,
assigned_worker_id null, matching claims/epoch preserved, Worker
active_jobs=0, Native slot no longer processing and Worker READY with
active_job_id=null. Re-read after a short hold to reject a late
succeeded/failed overwrite or new Job. Any missing claim, early Job
completion, failed Worker, unreleased slot, weak/unknown auth or
deadline fails the trial. Sampled GPU free must remain >=2048MiB
with reported max sample interval <=250ms; this does not prove
continuous or independently authenticated GPU origin.

Only after original8K Worker is restored n_ctx8192 and ownership/byte
hashes rechecked may the timer be disarmed and the isolated Control
stopped. Require no lab listeners on 19090/18085/19100 and read-only
check production Control/Deneb again. Keep previous receipts intact.

An observed lab cancellation cannot prove client disconnection after
the *first* model SSE text token: this test targets a claimed native
processing long-prefill request. Full after-first-token cancellation,
unrelated concurrent Jobs, production PostgreSQL Gateway, trusted
source/runtime/physical and wire provenance, fresh reviewed Serving
revision and client/catalog opt-in are separate acceptance gates.
