-- Append-only release records for audio epoch preparations.
BEGIN;

CREATE TABLE IF NOT EXISTS audio_epoch_preparation_releases (
    worker_id text NOT NULL,
    runtime_instance_epoch text NOT NULL
        CHECK (runtime_instance_epoch ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'),
    release_request_id text NOT NULL UNIQUE
        CHECK (release_request_id ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'),
    released_at timestamptz NOT NULL,
    PRIMARY KEY (worker_id, runtime_instance_epoch),
    FOREIGN KEY (worker_id, runtime_instance_epoch)
        REFERENCES audio_epoch_preparations(worker_id, runtime_instance_epoch)
        ON DELETE RESTRICT
);

COMMIT;
