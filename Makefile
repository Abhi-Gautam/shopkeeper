DUCKDB ?= duckdb
DB := store/kirana.duckdb
CSV := store/products.csv

.PHONY: catalog db serve phoenix clean

catalog: $(CSV)

$(CSV): store/seed_products.py
	python3 store/seed_products.py $(CSV)

db: $(DB)

$(DB): $(CSV) store/schema.sql
	rm -f $(DB)
	$(DUCKDB) $(DB) -c ".read store/schema.sql" -c "COPY products FROM '$(CSV)' (HEADER, AUTO_DETECT);" -c "SELECT count(*) AS products, count(*) FILTER (WHERE stock = 0) AS out_of_stock FROM products;"

# Blocks. Speaks MCP on stdin/stdout. One process owns the file.
serve: $(DB)
	$(DUCKDB) -unsigned -init store/publish.sql $(DB)

# Message viewer. UI http://localhost:6006  OTLP http://localhost:6006/v1/traces
phoenix:
	docker compose up -d

clean:
	rm -f $(DB) $(DB).wal $(CSV)
