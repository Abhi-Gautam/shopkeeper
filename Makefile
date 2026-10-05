DUCKDB ?= duckdb
DB := store/shop.duckdb
CSV := store/products.csv

.PHONY: db phoenix run clean

db: $(DB)

$(CSV): store/seed_products.py
	python3 store/seed_products.py $(CSV)

$(DB): $(CSV) store/schema.sql
	rm -f $(DB)
	$(DUCKDB) $(DB) -c ".read store/schema.sql" -c "COPY products FROM '$(CSV)' (HEADER, AUTO_DETECT);" -c "SELECT count(*) AS products, count(*) FILTER (WHERE stock = 0) AS out_of_stock FROM products;"

# Phoenix. UI http://localhost:6006  OTLP http://localhost:6006/v1/traces
phoenix:
	docker compose up -d

# Play, score and log a run. make run ARGS="--limit 10 --ui"
run: $(DB)
	.venv/bin/python floor/run.py $(ARGS)

clean:
	rm -f $(DB) $(DB).wal $(CSV)
