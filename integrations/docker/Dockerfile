# logsite alert-server: the error summary for Grafana (Error groups panel, history
# annotations, alert messages), with the history store on a volume.
#
#   docker build -t logsite integrations/docker
#   docker run -p 8085:8085 -v "$PWD:/repo:ro" -v logsite-history:/data \
#     -e LOGSITE_ALERT_TOKEN=... logsite
#
# /repo is your service's repository (code, logsite.toml and .git: for code lines and the
# commits that fixed an error), mounted read-only.
FROM python:3.13-slim

ARG LOGSITE_VERSION=
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir "logsite${LOGSITE_VERSION:+==$LOGSITE_VERSION}" \
    && git config --system --add safe.directory '*' \
    && useradd --create-home --uid 10001 logsite \
    && mkdir /data && chown logsite /data

USER logsite
ENV LOGSITE_HISTORY=/data/history.db
VOLUME /data
EXPOSE 8085
HEALTHCHECK CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8085/healthz')"
ENTRYPOINT ["logsite", "--config-dir", "/repo"]
CMD ["alert-server", "--host", "0.0.0.0", "--port", "8085", "--sync-every", "15m"]
