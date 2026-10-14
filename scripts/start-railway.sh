#!/bin/sh
# Railway start command: the API and the worker in one container, sharing
# the /data volume (documents and the local job queue).
#
# Exits as soon as either process stops, so the platform restarts the
# container instead of serving an API whose worker is gone. SIGTERM (every
# redeploy) stops both and exits 0.
#
# ponytail: one container, scaled up not out. Upgrade path: documents in an
# object store (S3 adapter), a shared queue (Postgres or Pub/Sub) and a
# separate worker service.
set -u

WORKER_CMD="${WORKER_CMD:-python -m invoice_processing.worker}"
# A bound on shutdown: uvicorn otherwise waits for every open connection.
API_CMD="${API_CMD:-uvicorn invoice_processing.api.service:app --host 0.0.0.0 --port ${PORT:-8000} --timeout-graceful-shutdown 20}"

sh -c "$WORKER_CMD" &
worker=$!
sh -c "$API_CMD" &
api=$!

stop() {
  kill "$worker" "$api" 2>/dev/null
  wait
}
trap 'stop; exit 0' TERM INT

# POSIX sh has no `wait -n`: check both every second.
while kill -0 "$worker" 2>/dev/null && kill -0 "$api" 2>/dev/null; do
  sleep 1
done
echo "start-railway: a process stopped; stopping the container" >&2
stop
exit 1
