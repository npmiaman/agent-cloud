# Agent Cloud server image.
#   docker build -t agentcloud .
#   docker run -p 8787:8787 -v agentcloud-data:/data agentcloud
# The first start creates a local admin and prints a one-time console login link in the logs.
FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY agentcloud_core ./agentcloud_core
COPY agentcloud_server ./agentcloud_server
COPY sdk/python ./sdk/python
RUN pip wheel --no-cache-dir --wheel-dir /wheels ".[fast]"

FROM python:3.12-slim
LABEL org.opencontainers.image.title="Agent Cloud" \
      org.opencontainers.image.description="Self-hosted cloud workspaces for AI agents" \
      org.opencontainers.image.source="https://github.com/npmiaman/agent-cloud" \
      org.opencontainers.image.licenses="Apache-2.0"
RUN useradd --create-home --uid 10001 agentcloud && mkdir /data && chown agentcloud /data
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels
USER agentcloud
VOLUME /data
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8787/healthz').status==200 else 1)"
ENTRYPOINT ["agentcloud"]
CMD ["up", "--host", "0.0.0.0", "--port", "8787", "--data", "/data", "--workers", "auto"]
