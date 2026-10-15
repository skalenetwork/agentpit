#!/usr/bin/env bash
# Snapshot the DB and deployments/local.json, the address book that ties it to
# the contracts. The chain itself is SKALE and cannot be snapshotted or rolled
# back: a restored dump is behind it by whatever settled after the backup. Run
# on the server; wire to cron when the stack settles.
#
#   ./deploy/backup.sh            # writes /root/backups/<project>-<UTC stamp>/
#
# The compose project is the one docker-compose.prod.yml names
# (agentpit); AGENTPIT_PROJECT overrides it. .env and local.json always
# come from this script's own clone, so back a project up from the clone that
# runs it.
set -euo pipefail

cd "$(dirname "$0")/.."

PROJECT="${AGENTPIT_PROJECT:-$(sed -n 's/^name: *//p' deploy/docker-compose.prod.yml)}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DEST="${BACKUP_DIR:-/root/backups}/$PROJECT-$STAMP"
COMPOSE=(docker compose -p "$PROJECT" -f deploy/docker-compose.prod.yml --env-file .env)

mkdir -p "$DEST"

"${COMPOSE[@]}" exec -T postgres pg_dump -U agentpit agentpit | gzip > "$DEST/agentpit.sql.gz"

cp deployments/local.json "$DEST/deployment.json"

echo "backup written: $DEST"
ls -lh "$DEST"
