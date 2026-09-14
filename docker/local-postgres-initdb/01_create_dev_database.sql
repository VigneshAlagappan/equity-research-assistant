-- Runs once, automatically, the first time docker-compose.test.yml's
-- container starts against a fresh volume (postgres's own docker-
-- entrypoint-initdb.d convention -- every .sql file here runs in filename
-- order, against POSTGRES_DB, only on initial data-directory creation).
--
-- signals_dev is the persistent local-dev database (seeded via
-- `python -m scripts.seed_local_dev_db`, kept across runs, never dropped
-- automatically) -- distinct from the ephemeral test_<uuid> databases
-- tests/postgres_test_db.py creates and drops per test.
CREATE DATABASE signals_dev;
