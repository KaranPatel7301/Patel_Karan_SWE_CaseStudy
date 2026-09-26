.PHONY: ingest dump test

ingest:
	docker compose exec -T api python -m app.ingest.cli all

dump:
	docker compose exec -T db pg_dump -U tracker --no-owner --no-acl tracker | gzip > seed/seed.sql.gz

test:
	docker compose exec -T api python -m pytest
