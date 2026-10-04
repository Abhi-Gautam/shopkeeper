DUCKDB ?= duckdb
DB := store/kirana.duckdb
CSV := store/products.csv

.PHONY: catalog db serve phoenix floor clean

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

# The picture. http://127.0.0.1:8787  Do not run beside the prompt batch.
floor: $(DB)
	.venv/bin/python floor/server.py

clean:
	rm -f $(DB) $(DB).wal $(CSV)
