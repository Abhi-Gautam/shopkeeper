DUCKDB ?= duckdb
DB := store/shop.duckdb
CSV := store/products.csv
# The Open Food Facts export, loaded once as text columns.
OFF := store/off/off.duckdb

.PHONY: db phoenix run clean

db: $(DB)

$(CSV): store/seed_off.sql $(OFF)
	$(DUCKDB) -readonly $(OFF) -c ".read store/seed_off.sql"

$(DB): $(CSV) store/schema.sql
	rm -f $(DB)
	$(DUCKDB) $(DB) -c ".read store/schema.sql" -c "COPY products FROM '$(CSV)' (HEADER, QUOTE '\"', ESCAPE '\"');" -c "INSTALL fts; LOAD fts;" -c "PRAGMA create_fts_index('products', 'sku', 'brand', 'name', 'item', 'pack_label', stemmer='english', ignore='(\\\\.|[^a-z0-9])+', overwrite=1);" -c "SELECT count(*) AS products, count(DISTINCT item) AS items, count(*) FILTER (WHERE stock = 0) AS out_of_stock FROM products;"

# Phoenix. UI http://localhost:6006  OTLP http://localhost:6006/v1/traces
phoenix:
	docker compose up -d

# Play the customers into Phoenix. make run ARGS="--limit 10 --ui"
run: $(DB)
	.venv/bin/python floor/run.py $(ARGS)

clean:
	rm -f $(DB) $(DB).wal $(CSV)
