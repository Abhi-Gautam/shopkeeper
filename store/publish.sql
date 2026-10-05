-- Curated MCP surface. Loaded by the server process, not by the seeder.
-- builtin tools stay off: the model must not get a raw SQL tool.

LOAD duckdb_mcp;

PRAGMA mcp_publish_tool(
    'guide',
    'Look up what the shop actually has. Does not change stock. Call this before talking about price, packs, or substitutes, and before buy when the customer has not named a SKU.',
    'SELECT sku, name, brand, category, pack_label,
            printf(''%.2f'', price_cents / 100.0) AS price_usd,
            printf(''%.2f'', list_cents / 100.0) AS list_usd,
            stock,
            stock > 0 AS in_stock
     FROM products
     WHERE (
         name ILIKE ''%'' || $query || ''%''
         OR brand ILIKE ''%'' || $query || ''%''
         OR category ILIKE ''%'' || $query || ''%''
         OR sku ILIKE ''%'' || $query || ''%''
       )
       AND ($category IS NULL OR category = $category)
     ORDER BY (stock = 0), price_cents
     LIMIT LEAST(GREATEST(COALESCE($limit, 8), 1), 12)',
    '{
        "query": {"type": "string", "description": "Words from the customer: item, brand, or SKU"},
        "category": {"type": "string", "description": "Optional exact category such as flour, grains, dairy, oil"},
        "limit": {"type": "integer", "description": "How many rows. Default 8, hard cap 12"}
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
