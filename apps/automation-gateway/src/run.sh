#!/bin/bash

# OpenTelemetry Configuration via Environment Variables
export OTEL_SERVICE_NAME="${OTEL_SERVICE_NAME:-fastapi-app}"
export OTEL_TRACES_EXPORTER="${OTEL_TRACES_EXPORTER:-otlp}"
export OTEL_METRICS_EXPORTER="${OTEL_METRICS_EXPORTER:-none}"  # Using Prometheus instead
export OTEL_LOGS_EXPORTER="${OTEL_LOGS_EXPORTER:-none}"     # Logs go to stdout, collected by Loki
# Tempo OTLP endpoint (using HTTP protocol on port 4318)
export OTEL_EXPORTER_OTLP_TRACES_ENDPOINT="${OTEL_EXPORTER_OTLP_TRACES_ENDPOINT:-http://tempo:4318/v1/traces}"
export OTEL_EXPORTER_OTLP_PROTOCOL="${OTEL_EXPORTER_OTLP_PROTOCOL:-http/protobuf}"

# Enable trace context injection into logs
export OTEL_PYTHON_LOG_CORRELATION="${OTEL_PYTHON_LOG_CORRELATION:-true}"

# Optional: Set sampling (1.0 = 100% of traces)
export OTEL_TRACES_SAMPLER="parentbased_always_on"

# Run with auto-instrumentation
exec opentelemetry-instrument uvicorn main:app --host 0.0.0.0 --port 8000
