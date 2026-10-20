# python:3.12-slim, pinned to the multi-arch index digest it pointed at on
# 2026-10-20. Dependabot (docker ecosystem) proposes digest bumps. To re-pin by
# hand: docker buildx imagetools inspect python:3.12-slim, and copy "Digest".
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Fixed uid/gid so volume ownership and Kubernetes-style runAsUser checks are stable.
RUN groupadd --system --gid 10001 saturn \
 && useradd --system --uid 10001 --gid saturn --no-create-home \
      --home-dir /nonexistent --shell /usr/sbin/nologin saturn

# Every package, including transitive ones, must match a hash in the lock.
# Wheels only, so no package runs setup.py at build time.
COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: -r requirements.txt

# Code stays owned by root and is read-only for the runtime user.
COPY . .
RUN chmod 0755 docker-entrypoint.sh

USER 10001:10001

EXPOSE 8000

ENTRYPOINT ["./docker-entrypoint.sh"]
