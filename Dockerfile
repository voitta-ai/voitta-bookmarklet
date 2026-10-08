# Voitta Compute in a container: the same backend start.sh runs, with the
# frontend bundle and the docs RAG index built into the image.
#
#   docker run -d --name voitta-compute \
#     -p 127.0.0.1:12358:12358 -p 127.0.0.1:12359:12359 \
#     -v voitta-compute-data:/data ghcr.io/voitta-ai/voitta-compute
#
# Publish the ports on 127.0.0.1 only: single-user mode has no login.
# Optional TLS: mount a mkcert pair at /app/backend/certs (see README).

# ---- frontend bundle --------------------------------------------------------
FROM node:20-slim AS frontend
WORKDIR /src
COPY frontend/package.json frontend/package-lock.json frontend/
RUN cd frontend && npm ci --silent
COPY frontend frontend
# widget.tsx globs ../../plugins/**/frontend/widget.ts into the bundle.
COPY plugins plugins
RUN cd frontend && npm run build

# ---- runtime ----------------------------------------------------------------
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 1000 voitta

WORKDIR /app
COPY backend/pyproject.toml backend/pyproject.toml
COPY backend/app backend/app
RUN python -m venv backend/.venv \
 && backend/.venv/bin/pip install --no-cache-dir -e backend

COPY docs docs
COPY plugins plugins
COPY scripts scripts
COPY start.sh start.sh
COPY --from=frontend /src/frontend/dist frontend/dist

# All mutable state lives under /data (one volume): conversations,
# projects, scripts, the agent engine's sessions, and settings.json, which
# the app reads from ~/.config/voitta-compute.
ENV VOITTA_DATA_ROOT=/data \
    VOITTA_HOST=0.0.0.0 \
    HOME=/home/voitta
RUN mkdir -p /data/config /home/voitta/.config \
 && ln -s /data/config /home/voitta/.config/voitta-compute \
 && chown -R voitta:voitta /app /data /home/voitta

USER voitta
# Build the docs RAG corpus now so first start is fast. lib-sources/ is not
# in the image, so the code corpus is skipped. build_all captures sys.stdout,
# so the log callback must write to the original stream (see server-start.sh).
RUN cd backend && .venv/bin/python -c "import sys; from app import rag_build; sys.exit(0 if rag_build.build_all(lambda l: sys.__stdout__.write(l + '\n')) else 1)"

VOLUME /data
EXPOSE 12358 12359
CMD ["./start.sh"]
