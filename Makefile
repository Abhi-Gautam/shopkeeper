DUCKDB ?= duckdb
DB := store/shop.duckdb
CSV := store/products.csv
BIGDB := store/big.duckdb
BIGCSV := store/big.csv
OFF := store/off/off.duckdb

.PHONY: db bigdb phoenix run clean

db: $(DB)

bigdb: $(BIGDB)

$(CSV): store/seed_products.py
	python3 store/seed_products.py $(CSV)

$(DB): $(CSV) store/schema.sql
	rm -f $(DB)
	$(DUCKDB) $(DB) -c ".read store/schema.sql" -c "COPY products FROM '$(CSV)' (HEADER, AUTO_DETECT);" -c "INSTALL fts; LOAD fts;" -c "PRAGMA create_fts_index('products', 'sku', 'brand', 'name', 'item', 'pack_label', stemmer='english', overwrite=1);" -c "SELECT count(*) AS products, count(*) FILTER (WHERE stock = 0) AS out_of_stock FROM products;"

# The Open Food Facts shelf. $(OFF) is the export loaded as text columns.
$(BIGCSV): store/seed_off.sql $(OFF)
	$(DUCKDB) -readonly $(OFF) -c ".read store/seed_off.sql"

$(BIGDB): $(BIGCSV) store/schema.sql
	rm -f $(BIGDB)
	$(DUCKDB) $(BIGDB) -c ".read store/schema.sql" -c "COPY products FROM '$(BIGCSV)' (HEADER, QUOTE '\"', ESCAPE '\"');" -c "INSTALL fts; LOAD fts;" -c "PRAGMA create_fts_index('products', 'sku', 'brand', 'name', 'item', 'pack_label', stemmer='english', overwrite=1);" -c "SELECT count(*) AS products, count(DISTINCT item) AS items, count(*) FILTER (WHERE stock = 0) AS out_of_stock FROM products;"

# Phoenix. UI http://localhost:6006  OTLP http://localhost:6006/v1/traces
phoenix:
	docker compose up -d

# Play, score and log a run. make run ARGS="--limit 10 --ui"
run: $(DB)
	.venv/bin/python floor/run.py $(ARGS)

clean:
	rm -f $(DB) $(DB).wal $(CSV) $(BIGDB) $(BIGDB).wal $(BIGCSV)
