-- Subscriber init: matching table + a subscription to the publisher.
-- This runs only after the publisher is healthy (publication exists), so the
-- CREATE SUBSCRIPTION connection succeeds on the first try.

CREATE TABLE IF NOT EXISTS orders (
    id          bigint PRIMARY KEY,
    customer    text        NOT NULL,
    amount_cents bigint     NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE SUBSCRIPTION app_sub
    CONNECTION 'host=pg-publisher port=5432 user=postgres password=postgres dbname=appdb'
    PUBLICATION app_pub
    WITH (copy_data = true, create_slot = true, slot_name = 'app_sub');
