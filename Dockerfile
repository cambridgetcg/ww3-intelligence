FROM python:3.14-slim@sha256:d3400aa122fa42cf0af0dbe8ec3091b047eac5c8f7e3539f7135e86d855dc015

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WW3_RUNTIME_UID=10001 \
    WW3_RUNTIME_GID=10001 \
    HOME=/tmp

WORKDIR /app

COPY requirements.txt requirements-dashboard.txt requirements-deploy.lock ./
RUN python -m pip install --no-cache-dir \
    -r requirements-dashboard.txt \
    -c requirements-deploy.lock

COPY analysis ./analysis
COPY cli ./cli
COPY core ./core
COPY dashboard ./dashboard
COPY ingestion ./ingestion
COPY interpretation ./interpretation
COPY ops ./ops
COPY prompts ./prompts
COPY registry ./registry
COPY spec ./spec
COPY ww3.py ./ww3.py

RUN chmod -R u=rwX,go=rX /app \
    && mkdir -p /data/stream \
    && chown -R 10001:10001 /data

EXPOSE 8080

CMD ["python", "-m", "ops.fly_runtime"]
