-- A Target-sized shelf from Open Food Facts: every US-only product with a
-- brand, a pack size and a category. Run against store/off/off.duckdb, which
-- holds the export's columns as text. Writes the same columns as
-- seed_products.py, so schema.sql and publish.sql do not change.
--
-- item is the product's detailed category ("Peanut butters"), category is
-- the top of its category path ("Plant-based foods and beverages"). Prices
-- and stock are not in the data, so they are made up from hashes: same
-- data, same shelf.

CREATE TEMP TABLE parsed AS
SELECT
    code,
    trim(product_name) AS name,
    trim(split_part(brands, ',', 1)) AS brand,
    main_category_en AS item,
    list_filter(string_split(categories_en, ','), lambda x: trim(x) NOT IN ('', 'Null'))[1] AS category,
    regexp_extract(lower(quantity),
        '(\d+(?:[.,]\d+)?)\s*(fl\.?\s*oz|oz|lbs?|kg|g|ml|cl|l|ct|count|pk|pack|bags?|pcs?|pieces?)\b',
        ['n', 'u']) AS m,
    TRY_CAST(unique_scans_n AS INT) AS scans,
    TRY_CAST(completeness AS DOUBLE) AS completeness
FROM products
WHERE countries_en = 'United States'
  AND brands IS NOT NULL AND product_name IS NOT NULL AND quantity IS NOT NULL
  AND main_category_en IS NOT NULL
  AND main_category_en NOT LIKE '%:%'
  AND main_category_en NOT IN ('Undefined', 'Groceries')
  AND quantity NOT ILIKE '%serving%';

CREATE TEMP TABLE sized AS
SELECT *,
    TRY_CAST(replace(m.n, ',', '.') AS DOUBLE) AS qty,
    CASE
        WHEN m.u LIKE 'fl%' THEN 'fl oz'
        WHEN m.u = 'oz' THEN 'oz'
        WHEN m.u LIKE 'lb%' THEN 'lb'
        WHEN m.u IN ('g', 'kg', 'ml') THEN m.u
        WHEN m.u = 'cl' THEN 'cl'
        WHEN m.u = 'l' THEN 'L'
        WHEN m.u <> '' THEN 'ct'
    END AS unit
FROM parsed
WHERE m.u <> '' AND category IS NOT NULL AND brand <> '' AND name <> '';

CREATE TEMP TABLE shelf AS
SELECT *,
    -- Rough grams, only to size prices and drop serving-size rows.
    qty * CASE unit WHEN 'fl oz' THEN 29.57 WHEN 'oz' THEN 28.35 WHEN 'lb' THEN 453.6
                    WHEN 'kg' THEN 1000 WHEN 'L' THEN 1000 WHEN 'cl' THEN 10
                    WHEN 'ct' THEN 50 ELSE 1 END AS grams,
    CASE WHEN unit = 'cl' THEN printf('%g ml', qty * 10)
         ELSE printf('%g %s', qty, unit) END AS pack_label
FROM sized
WHERE qty > 0;

COPY (
    WITH kept AS (
        SELECT * FROM shelf
        WHERE grams >= 50
        QUALIFY row_number() OVER (
            PARTITION BY lower(name), lower(brand), pack_label
            ORDER BY scans DESC NULLS LAST, completeness DESC NULLS LAST, code) = 1
    ),
    priced AS (
        SELECT *,
            -- Dollars for a typical pack of this item, then scaled by size and brand.
            (1.5 + hash(item) % 600 / 100.0)
              * least(greatest(pow(grams / median(grams) OVER (PARTITION BY item), 0.6), 0.4), 3)
              * (0.8 + hash(brand) % 50 / 100.0) AS dollars
        FROM kept
    ),
    -- A department with a handful of products is noise in the path, not an aisle.
    aisled AS (
        SELECT * REPLACE (CASE WHEN count(*) OVER (PARTITION BY category) < 20
                               THEN 'Other' ELSE category END AS category)
        FROM priced
    )
    SELECT
        code AS sku,
        name,
        item,
        brand,
        category,
        CASE WHEN unit = 'cl' THEN 'ml' ELSE unit END AS unit,
        CASE WHEN unit = 'cl' THEN qty * 10 ELSE qty END AS pack_qty,
        pack_label,
        greatest(99, CAST(round(dollars * 10) AS INT) * 10 - 1) AS price_cents,
        CASE WHEN hash(code || 'sale') % 100 < 15
             THEN CAST(round(greatest(99, CAST(round(dollars * 10) AS INT) * 10 - 1) * 1.2) AS INT)
             ELSE greatest(99, CAST(round(dollars * 10) AS INT) * 10 - 1) END AS list_cents,
        CASE WHEN hash(code || 'stock') % 100 < 7 THEN 0
             ELSE 2 + CAST(hash(code || 'qty') % 40 AS INT) END AS stock
    FROM aisled
    ORDER BY category, item, name
) TO 'store/products.csv' (HEADER);
