# AgentCore Runtime requires ARM64 at deployment; the documented build command
# supplies --platform linux/arm64 rather than hard-coding a Dockerfile value.
FROM public.ecr.aws/docker/library/python:3.11-slim AS builder

WORKDIR /build
RUN python -m venv /opt/texas-energy-venv
ENV PATH="/opt/texas-energy-venv/bin:${PATH}"
COPY pyproject.toml ./
COPY src ./src
COPY deployment/agentcore/requirements.txt ./deployment/agentcore/requirements.txt
# AgentCore microVMs are CPU runtimes. The public PyPI torch dependency can
# resolve CUDA toolkit wheels on Linux; install the official CPU-only ARM64
# wheel first, then add the remaining runtime dependencies without resolving
# a second torch distribution.
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu "torch==2.5.1" \
    && pip install --no-cache-dir -r deployment/agentcore/requirements.txt \
    && pip install --no-cache-dir --no-deps .

FROM public.ecr.aws/docker/library/python:3.11-slim

RUN useradd --system --uid 10001 --create-home agentcore
WORKDIR /app
COPY --from=builder /opt/texas-energy-venv /opt/texas-energy-venv
ENV PATH="/opt/texas-energy-venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1
USER agentcore
EXPOSE 8080
CMD ["uvicorn", "texas_energy.cloud.agentcore_entrypoint:app", "--host", "0.0.0.0", "--port", "8080"]
