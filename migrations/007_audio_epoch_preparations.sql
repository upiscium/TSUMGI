-- Durable, non-admitting preparation records for qualified audio epochs.
BEGIN;

CREATE TABLE IF NOT EXISTS audio_preparation_qualifications (
    qualification_revision text PRIMARY KEY
        CHECK (qualification_revision ~ '^sha256:[0-9a-f]{64}$'),
    worker_id text NOT NULL REFERENCES workers(id) ON DELETE RESTRICT,
    body jsonb NOT NULL CHECK (jsonb_typeof(body) = 'object')
);

CREATE TABLE IF NOT EXISTS audio_preparation_qualification_revocations (
    qualification_revision text PRIMARY KEY
        REFERENCES audio_preparation_qualifications(qualification_revision)
        ON DELETE RESTRICT,
    revoked_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS audio_epoch_preparations (
    worker_id text NOT NULL REFERENCES workers(id) ON DELETE RESTRICT,
    runtime_instance_epoch text NOT NULL
        CHECK (runtime_instance_epoch ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'),
    qualification_revision text NOT NULL
        REFERENCES audio_preparation_qualifications(qualification_revision) ON DELETE RESTRICT,
    deployment_revision text NOT NULL CHECK (deployment_revision ~ '^sha256:[0-9a-f]{64}$'),
    serving_contract_revision text NOT NULL CHECK (serving_contract_revision ~ '^sha256:[0-9a-f]{64}$'),
    prepare_request_id text NOT NULL UNIQUE
        CHECK (prepare_request_id ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'),
    expected_intent_revision bigint NOT NULL CHECK (expected_intent_revision >= 0),
    intent_revision bigint NOT NULL CHECK (intent_revision = expected_intent_revision + 1),
    prepared_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL CHECK (expires_at > prepared_at),
    PRIMARY KEY (worker_id, runtime_instance_epoch),
    UNIQUE (worker_id, intent_revision)
);

COMMIT;
