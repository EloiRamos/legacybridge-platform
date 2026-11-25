#!/bin/sh
# First-start initialization of the PostgreSQL cluster (runs as the bootstrap superuser).
# Creates the three least-privilege roles and the application database.
set -eu
MIGRATOR_PW="$(cat /run/secrets/db_migrator)"
APP_PW="$(cat /run/secrets/db_app)"
READER_PW="$(cat /run/secrets/db_audit_reader)"
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres <<SQL
CREATE ROLE lbf_migrator LOGIN PASSWORD '${MIGRATOR_PW}' CREATEDB;
CREATE ROLE lbf_app LOGIN PASSWORD '${APP_PW}';
CREATE ROLE lbf_audit_reader LOGIN PASSWORD '${READER_PW}';
ALTER ROLE lbf_app SET search_path = lbf;
ALTER ROLE lbf_audit_reader SET search_path = lbf;
ALTER ROLE lbf_migrator SET search_path = lbf, public;
CREATE DATABASE lbf OWNER lbf_migrator;
REVOKE ALL ON DATABASE lbf FROM PUBLIC;
GRANT CONNECT ON DATABASE lbf TO lbf_app, lbf_audit_reader;
SQL
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname lbf <<SQL
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
SQL
