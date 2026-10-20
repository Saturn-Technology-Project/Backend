#!/bin/sh
set -e

# Production runs migrations once per release in a Cloud Run Job and sets
# RUN_MIGRATIONS=false on the services. Local compose and any deploy that has
# not switched to the job keep migrating on start; alembic/env.py takes an
# advisory lock, so concurrent starts serialise instead of racing.
if [ "${RUN_MIGRATIONS:-true}" != "false" ]; then
  alembic upgrade head
fi

# No --proxy-headers / --forwarded-allow-ips: uvicorn then trusts
# X-Forwarded-* only from 127.0.0.1, so a client cannot spoof its address or
# scheme through those headers. The app reads the edge headers it needs itself.
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 "$@"
