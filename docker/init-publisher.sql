-- Publisher init: a small table with a primary key (so UPDATE/DELETE replicate)
-- and a publication that carries it.

CREATE TABLE IF NOT EXISTS orders (
    id          bigint PRIMARY KEY,
    customer    text        NOT NULL,
    amount_cents bigint     NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- A second table left OUT of the publication on purpose, so
-- check_publication_coverage has a real "expected but not published" gap to
-- find in the demo.
CREATE TABLE IF NOT EXISTS audit_log (
    id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    message text NOT NULL
);

INSERT INTO orders (id, customer, amount_cents) VALUES
    (1, 'acme',   1000),
    (2, 'globex', 2500),
    (3, 'initech', 9900)
ON CONFLICT (id) DO NOTHING;

-- The publisher healthcheck waits for this object to exist.
CREATE PUBLICATION app_pub FOR TABLE orders;
