# acp public-demo image: FastAPI control plane serving the React SPA same-origin,
# with both analysis engines bundled as COMPILED artifacts (no diligence source):
#   - .NET 10 Office CLI  (spike/dotnet/.../Release: AcpScan.Cli + DigitalA11y.*.dll)
#   - Python PDF analyser (engine/pdf-analyser: analysers/ + models/, straight from the tree)
# Build context is the acp/ repo root.  Base images via mirror.gcr.io to dodge
# the docker.io 429s the Temporal standup hit.

# ---- base images ----------------------------------------------------------------------
# Both default to the upstream images, so `az acr build -f deploy/public/Dockerfile .` still
# works from scratch with no bases in the registry — a first deploy, a fork, or anyone who has
# never run redeploy.sh gets exactly the old behaviour.
#
# redeploy.sh overrides them with acp-base-web:<hash> / acp-base-api:<hash>, which already
# contain node_modules and the apt+.NET+pip layers. That is where the build time went: the apt
# block pulls LibreOffice and downloads the .NET 10 runtime, and `az acr build` has no
# --cache-from (az 2.86.0), so every QuickRun re-ran all of it for a one-line source change.
#
# The hash is over the dependency inputs only. When it does not move, the base is reused; when
# it moves, redeploy.sh rebuilds the base first. Correctness does not depend on remembering to.
ARG BASE_WEB=mirror.gcr.io/library/node:20-slim
ARG BASE_API=mirror.gcr.io/library/python:3.12-slim

# ---- stage 1: build the SPA (same-origin → relative API base) ----
FROM ${BASE_WEB} AS web
WORKDIR /web
# VITE_SIM=false → the SPA calls the real backend (scans persist to Postgres +
# emit Langfuse traces). Default false for this deployed image; override with
# --build-arg VITE_SIM=true to ship the offline synthetic-data demo instead.
ARG VITE_SIM=false
# Present in the base image; re-copied so a from-scratch build (BASE_WEB unset) still resolves
# deps. When BASE_WEB is a prebuilt base these are already satisfied and npm install is a no-op
# against the existing node_modules.
# frontend/ — the SPA this image serves, and now the ONLY SPA tree: the v2 redesign replaced
# the original `frontend/` in place, so `frontend-v2/` no longer exists. This image already
# served v2 before the swap; the rename is what makes the path here and the one Netlify builds
# the same tree, rather than two deployments quietly serving different apps.
#
# Three places name the tree and they must agree: here, Dockerfile.base-web, and redeploy.sh's
# WEB_HASH. The guard below tests only that node_modules EXISTS, so a base built from a
# DIFFERENT lockfile would satisfy it and the install would be skipped — the SPA would then
# build against the other tree's dependencies, with no error. WEB_HASH hashes the lockfile, so
# keeping all three on the same path is what makes a mismatch impossible rather than merely
# unlikely. (With one tree left there is no other lockfile to drift onto, but the guard's
# weakness is unchanged and the three references still have to move together.)
COPY frontend/package.json frontend/package-lock.json ./
RUN [ -d node_modules ] || npm install --prefer-offline 2>/dev/null || npm install
COPY frontend/ ./
RUN printf 'VITE_API=\nVITE_SIM=%s\n' "$VITE_SIM" > .env.production && npm run build   # -> /web/dist

# ---- stage 2: runtime ----
FROM ${BASE_API} AS runtime
WORKDIR /app

# apt + .NET + pip live in BASE_API. /app/.acp-base-ready is the marker it stamps; when it is
# present this whole block is skipped, which is the entire point — it is ~3 minutes of
# LibreOffice, a downloaded .NET runtime, and a pip resolve. When BASE_API is the bare upstream
# python image the marker is absent and the from-scratch path runs unchanged.
COPY api/requirements.txt /app/api/requirements.txt
RUN if [ ! -f /app/.acp-base-ready ]; then \
      apt-get update \
      && apt-get install -y --no-install-recommends curl ca-certificates libicu-dev tesseract-ocr \
         libreoffice-impress libreoffice-writer libreoffice-calc \
         fonts-liberation fonts-dejavu-core \
         libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b libfontconfig1 \
      && curl -fsSL https://dot.net/v1/dotnet-install.sh -o /tmp/dotnet-install.sh \
      && bash /tmp/dotnet-install.sh --runtime dotnet --channel 10.0 --install-dir /usr/share/dotnet \
      && ln -s /usr/share/dotnet/dotnet /usr/local/bin/dotnet \
      && rm /tmp/dotnet-install.sh \
      && apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/* \
      && pip install --no-cache-dir -r /app/api/requirements.txt; \
    fi

# app code + config + bundled sample corpus
COPY api/ /app/api/
COPY experiments/ /app/experiments/
ENV PYTHONPATH=/app:/app/api
COPY scripts/ /app/scripts/
COPY performance/ /app/performance/
COPY config/ /app/config/
COPY hub/ /app/hub/
COPY test-corpus/files/ /app/test-corpus/files/
# The demo fixtures land in the SAME directory because that is the one a local scan reads
# (api/scanner.py: ACP_LOCAL_CORPUS or test-corpus/files). Without them the bundled corpus is a
# single .html file, so a local scan exercises the HTML analyser and nothing else — the .NET
# Office CLI and the PDF analyser ship in this image and are never once executed by it. An
# engine that is bundled but unreachable passes every local check by not running. The
# non-document files here (README.md) are ignored: the scanner filters on OFFICE + .pdf + HTML.
COPY demo-fixtures/ /app/test-corpus/files/

# compiled engines (no source)
COPY spike/dotnet/AcpScan.Cli/bin/Release/net10.0/ /app/engine/office/
# Straight from the tracked tree (ADR 0029). This used to copy deploy/public/vendor/worker-python,
# a gitignored staging directory the deploy script filled from a checkout OUTSIDE the repo — so the
# build context depended on a path that existed on one laptop, and an expired token could make the
# copy a silent 0-module no-op that still built a working-looking image with no PDF engine in it.
COPY engine/pdf-analyser/ /app/engine/worker-python/

# built SPA
COPY --from=web /web/dist/ /app/static/

ENV ACP_DOTNET=/usr/local/bin/dotnet \
    ACP_OFFICE_CLI=/app/engine/office/AcpScan.Cli.dll \
    ACP_PDF_ENGINE=/app/engine/worker-python \
    ACP_STATIC_DIR=/app/static \
    DOTNET_CLI_TELEMETRY_OPTOUT=1 \
    DOTNET_NOLOGO=1 \
    PORT=8077
EXPOSE 8077
# Build provenance — passed by deploy.sh / redeploy.sh via az acr build; surfaced in /healthz
# and the UI. BUILD_SHA defaults EMPTY, not to a placeholder: an image built by a bare
# `docker build` has no commit to name, and /healthz reports that as null rather than as a
# commit nobody can look up. (BUILD_VERSION's `dev` default is different on purpose — it is the
# signal `version_stamped` reads to refuse an unstamped image.)
ARG BUILD_VERSION=dev
ARG BUILD_TIME=
ARG BUILD_SHA=
ENV ACP_BUILD_VERSION=$BUILD_VERSION \
    ACP_BUILD_TIME=$BUILD_TIME \
    ACP_BUILD_SHA=$BUILD_SHA
WORKDIR /app/api
# Worker-tier launcher (#113): a SINGLE dash-free command token for `az containerapp --command
# acp-worker` — az's --command can't carry "-c" or a JSON array (see worker-entry.sh header).
COPY deploy/public/worker-entry.sh /usr/local/bin/acp-worker
RUN chmod +x /usr/local/bin/acp-worker
# Materialize the Drive ADC secret (if provided) to a file google-auth can read,
# then start the server.
CMD ["sh", "-c", "if [ -n \"$ACP_GOOGLE_ADC\" ]; then printf '%s' \"$ACP_GOOGLE_ADC\" > /tmp/adc.json && export GOOGLE_APPLICATION_CREDENTIALS=/tmp/adc.json; fi; exec python process_main.py"]
