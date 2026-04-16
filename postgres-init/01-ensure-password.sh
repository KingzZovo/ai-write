#!/bin/bash
# Ensure postgres password matches POSTGRES_PASSWORD env var on every startup.
# This fixes the issue where password gets out of sync after volume recreation.
set -e

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    ALTER USER ${POSTGRES_USER} WITH PASSWORD '${POSTGRES_PASSWORD}';
EOSQL
