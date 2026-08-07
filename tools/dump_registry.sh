#!/usr/bin/env bash
# Dump an Agent Estate registry to the JSON shape tools/publish.py reads.
#
# This script is the only store-specific piece of the publishing path. The
# publisher itself stays stdlib-only and store-agnostic; if your registry
# lives somewhere else, replace this file and nothing else.
#
# This implementation reads Postgres through the running container, so it
# needs no database driver on the host and never exposes the port.
#
#   PGCONTAINER=ai-platform-postgres-1 PGUSER=ai_admin PGDATABASE=haluk \
#     tools/dump_registry.sh > /tmp/registry.json

set -euo pipefail

CONTAINER="${PGCONTAINER:-ai-platform-postgres-1}"
USER_NAME="${PGUSER:-postgres}"
DATABASE="${PGDATABASE:-haluk}"

read -r -d '' QUERY <<'SQL' || true
SELECT json_build_object(
  'projects', COALESCE((SELECT json_agg(row_to_json(p)) FROM (
      SELECT id, name, type, status FROM projects) p), '[]'::json),
  'agents', COALESCE((SELECT json_agg(row_to_json(a)) FROM (
      SELECT id, name, role, model, capabilities, status
      FROM agents ORDER BY id) a), '[]'::json),
  'tasks', COALESCE((SELECT json_agg(row_to_json(t)) FROM (
      SELECT id, title, status, created_at FROM tasks
      ORDER BY created_at DESC) t), '[]'::json),
  'findings', COALESCE((SELECT json_agg(row_to_json(f)) FROM (
      SELECT id, kind, title, severity, confidence, status, created_at
      FROM findings ORDER BY created_at DESC) f), '[]'::json),
  'signals', COALESCE((SELECT json_agg(row_to_json(s)) FROM (
      SELECT id, kind, metric, value, observed_at FROM signals
      ORDER BY observed_at DESC) s), '[]'::json)
)::text
SQL

# Note what is *not* selected: signal payloads, finding detail and proposal,
# task input, environment notes. The projection would drop them anyway, but a
# dump that never contains them cannot leak them through a stray debug print.
docker exec -i "$CONTAINER" psql -U "$USER_NAME" -d "$DATABASE" -Atc "$QUERY"
