-- Kirana inventory. Money is integer paise so a sale cannot drift a rupee.
-- stock is units of pack_label (one row of "5 kg atta" with stock 12 is
-- twelve bags, not 60 kg).

CREATE TABLE IF NOT EXISTS products (
    sku VARCHAR PRIMARY KEY,
    name VARCHAR NOT NULL,
    brand VARCHAR NOT NULL,
    category VARCHAR NOT NULL,
    unit VARCHAR NOT NULL,
    pack_qty DOUBLE NOT NULL,
    pack_label VARCHAR NOT NULL,
    price_paise INTEGER NOT NULL,
    mrp_paise INTEGER NOT NULL,
    stock INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sales (
    request_id VARCHAR PRIMARY KEY,
    sku VARCHAR NOT NULL,
    qty INTEGER NOT NULL,
    unit_price_paise INTEGER NOT NULL,
    sold_at TIMESTAMP NOT NULL DEFAULT now()
);
