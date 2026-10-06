-- Curated MCP surface. Loaded by the server process, not by the seeder.
-- builtin tools stay off: the model must not get a raw SQL tool.

-- stdout is the MCP stream. Drop the CLI's own result tables so a client
-- that reads every line as JSON-RPC never sees "0 rows".
.mode trash

LOAD duckdb_mcp;
LOAD fts;

-- Ranked word search over brand, name, item and pack, from the BM25 index
-- the Makefile builds. Words match on their own, so "Folgers decaf" finds
-- "Classic Decaf" by Folgers, and numbers are words too, so "21" lifts the
-- 21 oz box. No category filter: the model guessed categories, and an exact
-- filter on a wrong guess hid the right product.
PRAGMA mcp_publish_tool(
    'guide',
    'Search the shelf by words: brand, product, flavor, item. Returns the best matches first, one row per product and pack size, with price and stock. Does not change stock. Call it before talking about price, packs, or substitutes, and before buy. One item per call; call it several times at once for several items.',
    'SELECT sku, brand, name, item, category, pack_label,
            printf(''%.2f'', price_cents / 100.0) AS price_usd,
            printf(''%.2f'', list_cents / 100.0) AS list_usd,
            stock,
            stock > 0 AS in_stock
     FROM (
         SELECT *, fts_main_products.match_bm25(sku, $query) AS score
         FROM products
     )
     WHERE score IS NOT NULL
     QUALIFY row_number() OVER (
         PARTITION BY lower(brand), lower(name), pack_label
         ORDER BY (stock = 0), price_cents) = 1
     ORDER BY score DESC, (stock = 0), pack_qty
     LIMIT LEAST(GREATEST(COALESCE($limit, 10), 1), 20)',
    '{
        "query": {"type": "string", "description": "Words for one item: brand and product, like \"heinz ketchup\" or \"decaf coffee\". A size number helps, like \"21\" for 21 oz."},
        "limit": {"type": "integer", "description": "How many rows. Default 10, hard cap 20"}
    }',
    '["query"]',
    'markdown'
);

-- One transaction. Replay of request_id is a no-op on stock.
-- taken is the only row the UPDATE and INSERT are allowed to see.
PRAGMA mcp_publish_execution_tool(
    'buy',
    'Sell a known SKU if stock covers the quantity. Pass the SKU from guide, not a guessed name. Same request_id twice does not sell twice.',
    'BEGIN TRANSACTION;
     CREATE OR REPLACE TEMP TABLE taken AS
       SELECT sku, price_cents, stock AS stock_before
       FROM products
       WHERE sku = $sku
         AND $qty >= 1
         AND stock >= $qty
         AND NOT EXISTS (SELECT 1 FROM sales WHERE request_id = $request_id);
     UPDATE products
        SET stock = stock - $qty
      WHERE sku = $sku
        AND EXISTS (SELECT 1 FROM taken);
     INSERT INTO sales (request_id, sku, qty, unit_price_cents)
     SELECT $request_id, sku, $qty, price_cents FROM taken;
     COMMIT;
     SELECT
       CASE
         WHEN $qty < 1 THEN ''bad_qty''
         WHEN EXISTS (SELECT 1 FROM taken) THEN ''sold''
         WHEN EXISTS (SELECT 1 FROM sales WHERE request_id = $request_id) THEN ''sold''
         WHEN NOT EXISTS (SELECT 1 FROM products WHERE sku = $sku) THEN ''unknown_sku''
         ELSE ''out_of_stock''
       END AS status,
       $request_id AS request_id,
       $sku AS sku,
       COALESCE((SELECT qty FROM sales WHERE request_id = $request_id), 0) AS qty_sold,
       (SELECT printf(''%.2f'', price_cents / 100.0) FROM products WHERE sku = $sku) AS price_usd,
       (SELECT stock FROM products WHERE sku = $sku) AS stock_left;',
    '{
        "sku": {"type": "string", "description": "Exact SKU from guide"},
        "qty": {"type": "integer", "description": "Packs to sell, at least 1"},
        "request_id": {"type": "string", "description": "Unique id for this buy attempt. Reuse it on retry."}
    }',
    '["sku", "qty", "request_id"]',
    '[
        {},
        {"sku": "string", "qty": "integer", "request_id": "string"},
        {"sku": "string", "qty": "integer"},
        {"request_id": "string", "qty": "integer"},
        {},
        {"request_id": "string", "sku": "string", "qty": "integer"}
    ]'
);

PRAGMA mcp_server_start('stdio', 'localhost', 0, '{"builtin_tools": false}');
