# syntax=docker/dockerfile:1
# Paper / replay image. Contains no credentials; live trading stays disabled.
# Behind a TLS-inspecting proxy, pass its CA for the build only (it is not stored in the image):
#   docker build --secret id=pip_ca,src=/path/to/ca-bundle.crt -t spotguard .
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN useradd --create-home --uid 10001 spotguard
WORKDIR /app

COPY requirements.lock ./
RUN --mount=type=secret,id=pip_ca,required=false \
    if [ -f /run/secrets/pip_ca ]; then export PIP_CERT=/run/secrets/pip_ca; fi; \
    pip install --no-cache-dir -r requirements.lock

COPY pyproject.toml README.md IMPLEMENTATION_MATRIX.md policy.toml ./
COPY src ./src
COPY tests ./tests
RUN --mount=type=secret,id=pip_ca,required=false \
    if [ -f /run/secrets/pip_ca ]; then export PIP_CERT=/run/secrets/pip_ca; fi; \
    pip install --no-cache-dir --no-deps .

USER spotguard
WORKDIR /data
# Default: deterministic offline replay. Paper on live data: `docker run ... spotguard --policy /app/policy.toml paper`
ENTRYPOINT ["spotguard", "--policy", "/app/policy.toml", "--db", "/data/spotguard.sqlite"]
CMD ["replay", "--data", "/data/replay.jsonl", "--generate", "--minutes", "60"]
