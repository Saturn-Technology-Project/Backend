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
#
# --no-access-log: the app writes its own line per request, with a request id and without the
# query string. uvicorn's access log would repeat it with OAuth codes and state values included.
# --timeout-graceful-shutdown: on a new revision Cloud Run sends SIGTERM and waits 10 seconds.
# Open event streams never finish on their own, so without a limit the process is killed while
# a tool call may be in flight; with one, streams are closed and other requests get to finish.
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --no-access-log --timeout-graceful-shutdown 8 "$@"
