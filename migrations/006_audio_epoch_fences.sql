-- Durable, negative-only revocation records for audio runtime epochs.
BEGIN;

CREATE TABLE IF NOT EXISTS audio_epoch_fences (
    worker_id text NOT NULL REFERENCES workers(id) ON DELETE RESTRICT,
    runtime_instance_epoch text NOT NULL
        CHECK (runtime_instance_epoch ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'),
    deployment_revision text NOT NULL
        CHECK (deployment_revision ~ '^sha256:[0-9a-f]{64}$'),
    serving_contract_revision text NOT NULL
        CHECK (serving_contract_revision ~ '^sha256:[0-9a-f]{64}$'),
    fence_request_id text NOT NULL UNIQUE
        CHECK (fence_request_id ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'),
    fenced_at timestamptz NOT NULL,
    PRIMARY KEY (worker_id, runtime_instance_epoch)
);

COMMIT;
