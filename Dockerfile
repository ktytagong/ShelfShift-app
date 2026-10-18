# The app image: the API plus the built web app. The same image runs the deploy job (python -m api.sandbox build,
# activate and restore, and the baseline briefs job python -m agent.briefs) and the benchmark jobs (python -m api.bench); the service command below never migrates or builds a template
# (OPS-05). Packages come only from the approved locks: web/package-lock.json (npm ci) and requirements.lock.

# Stage 1: the production web build (tsc, then vite build) into /web/dist.
FROM node:24-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

# Stage 2: the runtime. Only what the app reads ships: engine, data, api, reference (api/config.py reads reference/ at
# import and refdata.install compares its fingerprint), the agent package (readyz's access checks import agent/health.py
# with GCP on, 11-CONTEXT D-01, D-13; Phase 9's assistant, its tools and the baseline briefs job, `python -m agent.briefs`)
# and the web build. Never tests or the deploy folder (06-CONTEXT D-18).
FROM python:3.12-slim
# No .pyc files in the image; logs arrive line by line in the job and service logs.
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock
COPY engine engine
COPY data data
COPY api api
COPY agent agent
COPY reference reference
COPY --from=web /web/dist web/dist
# A system user with no login shell: the app never runs as root.
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin shelfshift
USER 10001
EXPOSE 8080
# One uvicorn process on $PORT (Cloud Run sets it; 8080 locally). Proxy headers stay off (06-CONTEXT D-24), so no
# client-written X-Forwarded-For ever replaces the TCP peer; on Cloud Run the app reads the rightmost entry itself.
CMD ["sh", "-c", "exec uvicorn api.app:app --host 0.0.0.0 --port \"${PORT:-8080}\" --no-proxy-headers"]
