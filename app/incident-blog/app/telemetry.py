"""OpenTelemetry: traces and logs to the OpenTelemetry Collector in the cluster (OTLP over HTTP).

Off unless OTEL_EXPORTER_OTLP_ENDPOINT is set (the Helm chart sets it), so tests, DAST and local runs
send nothing. Everything else is the standard OTEL_* environment: OTEL_SERVICE_NAME,
OTEL_RESOURCE_ATTRIBUTES, OTEL_TRACES_SAMPLER and OTEL_TRACES_SAMPLER_ARG.
  traces  every request (FastAPI) and every SQL statement (SQLAlchemy engine events)
  logs    the app's own log records, carrying the trace and span id of the request that wrote them,
          so Grafana jumps from a trace to its logs and back
Health, readiness and metrics endpoints are not traced: probes and scrapes would drown the real traffic.
The collector forwards traces to Tempo and logs to Loki.
"""

import logging
import os
from dataclasses import dataclass

EXCLUDED_URLS = "healthz,readyz,metrics"
_trace_engine = None   # set by setup(): attaches SQL spans to one SQLAlchemy engine


@dataclass
class Telemetry:
    tracer_provider: object
    logger_provider: object
    handler: logging.Handler


def setup(app, *, loggers=(), redis=False, span_exporter=None, log_exporter=None):
    """Instrument the app and return the providers, or None when telemetry is off.

    span_exporter / log_exporter replace the OTLP exporters (the tests pass in-memory ones).
    """
    if span_exporter is None and not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return None

    from opentelemetry._logs import set_logger_provider
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.logging.handler import LoggingHandler
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.trace import set_tracer_provider

    if span_exporter is None:
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        span_exporter, log_exporter = OTLPSpanExporter(), OTLPLogExporter()

    resource = Resource.create()   # service.name and the rest from OTEL_SERVICE_NAME / OTEL_RESOURCE_ATTRIBUTES
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
    set_tracer_provider(tracer_provider)
    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
    set_logger_provider(logger_provider)

    FastAPIInstrumentor.instrument_app(app, tracer_provider=tracer_provider, excluded_urls=EXCLUDED_URLS)
    _enable_sql_tracing(tracer_provider)
    if redis:
        from opentelemetry.instrumentation.redis import RedisInstrumentor
        RedisInstrumentor().instrument(tracer_provider=tracer_provider)

    # Only the app's own loggers (not the root): the exporter's own warnings must not feed back into it
    handler = LoggingHandler(level=logging.INFO, logger_provider=logger_provider)
    for name in loggers:
        logging.getLogger(name).addHandler(handler)
    return Telemetry(tracer_provider, logger_provider, handler)


def _enable_sql_tracing(tracer_provider):
    # Per engine (trace_engine), not SQLAlchemyInstrumentor().instrument(): on SQLAlchemy 2.1 that also patches
    # create_async_engine, whose import needs greenlet (optional since 2.1), and it declares "sqlalchemy < 2.1".
    # EngineTracer only listens to the engine's cursor events, which 2.1 keeps (tests/test_telemetry.py).
    global _trace_engine
    from opentelemetry.instrumentation.sqlalchemy.engine import EngineTracer
    from opentelemetry.instrumentation.sqlalchemy.version import __version__
    from opentelemetry.metrics import get_meter
    from opentelemetry.trace import get_tracer

    scope = "opentelemetry.instrumentation.sqlalchemy"
    tracer = get_tracer(scope, __version__, tracer_provider)
    connections = get_meter(scope, __version__).create_up_down_counter("db.client.connections.usage", unit="connections")
    _trace_engine = lambda engine: EngineTracer(tracer, engine, connections)  # noqa: E731


def trace_engine(engine):
    """Spans for every SQL statement of this engine; nothing while telemetry is off."""
    if _trace_engine is not None:
        _trace_engine(engine)
    return engine
