# Worker Offline recovery — guarded Control reconciliation (#162)

A Worker that loses Control acknowledgement stays fail-closed. Heartbeats
never transition an Offline Worker to Online or reacquire a GPU. The Worker
remains alive and serves local health diagnostics but cannot claim new Jobs.

## Recovery eligibility

Starting with this change, Control records the reason for a Worker heartbeat
TTL expiry in the existing metadata key
_tsumgi_control_offline_reason=heartbeat_timeout (Control-managed only).
An administrative OFFLINE transition instead sets operator. The metadata
key is reserved; Worker-submitted registration and heartbeat metadata carrying
it are rejected.

An idle Worker that receives an authenticated acknowledgement indicating
Offline due to TTL expiry may, after a cooldown (default 30 seconds), send
a separate authenticated POST /v1/workers/register with recovery=true.
This is explicitly a registration, not an implicit Heartbeat state change.

Control accepts only if, within the ownership transaction:
- An existing Worker record is still Offline and TTL-marked recoverable.
- The complete Worker topology, GPU UUIDs, max concurrency and serving
  declaration including runtime instance epoch match the existing record.
- Both recorded active-job capacity and durable running Job leases are zero.
- Serial GPU ownership checks find no other active claimant.

Otherwise Control returns 409, and the Worker remains not-ready with no claim.
The runtime serializes Heartbeat, reconcile and claim through the existing
Control lock; it suppresses recovery during drain/stop, an active Job, or
unhealthy native serving; retries are rate limited. HTTP 0/502/503 do
not automatically grant Online state.

## Compatibility and deployment

Legacy Control that does not emit the TTL marker is not automatically
recoverable. Existing workers already Offline from earlier versions do not
automatically become eligible merely because the software was upgraded.
They require separately approved safe re-registration or a controlled
restart after GPU/lease verification.

Deploy Control support before installing updated Workers. Validate against
an isolated PostgreSQL and test Worker before any production service cutover.
Do not assume that HTTP 200 from /health implies admission readiness.
