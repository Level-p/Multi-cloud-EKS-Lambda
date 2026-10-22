"""OpenTelemetry: a request becomes one trace (HTTP and SQL spans) and its log lines carry the trace id."""
import logging

import pytest
from fastapi.testclient import TestClient
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.sqlalchemy.engine import EngineTracer
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind

import app.main as blog
from app import telemetry
from app.db import make_session_factory

USER = {"X-Auth-Request-Email": "oncall@agripulse.example"}


def test_off_without_an_endpoint(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    assert telemetry.setup(blog.app) is None


@pytest.fixture
def otel(monkeypatch):
    monkeypatch.setenv("CSRF_SECRET", "s")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "incident-blog")
    # In the pod setup() runs at import, before the app serves anything; other tests may have started it already
    blog.app.middleware_stack = None
    spans, logs = InMemorySpanExporter(), InMemoryLogRecordExporter()
    t = telemetry.setup(blog.app, loggers=("incident-blog",), span_exporter=spans, log_exporter=logs)
    blog._sessions = make_session_factory("sqlite://")     # created after instrumenting: its SQL is traced
    yield t, spans, logs
    FastAPIInstrumentor.uninstrument_app(blog.app)
    blog.app.middleware_stack = None
    EngineTracer.remove_all_event_listeners()
    telemetry._trace_engine = None
    logging.getLogger("incident-blog").removeHandler(t.handler)
    blog._sessions = None


def test_request_is_one_trace_with_sql_and_logs(otel, caplog):
    t, spans, logs = otel
    caplog.set_level(logging.INFO, logger="incident-blog")   # in the pod logging.basicConfig sets INFO; pytest pre-empts it
    client = TestClient(blog.app)
    r = client.post("/incidents", data={"csrf": blog.csrf_token("oncall@agripulse.example"), "title": "Traced outage",
                                        "severity": "SEV2", "services": "web", "summary": "x"},
                    headers=USER, follow_redirects=False)
    assert r.status_code == 303, r.text
    assert client.get("/healthz").status_code == 200              # probes are not traced
    t.tracer_provider.force_flush()
    t.logger_provider.force_flush()

    finished = spans.get_finished_spans()
    servers = [s for s in finished if s.kind == SpanKind.SERVER]
    assert [s.attributes.get("http.route") for s in servers] == ["/incidents"]
    declare = servers[0]
    assert declare.resource.attributes["service.name"] == "incident-blog"
    assert any(s.instrumentation_scope.name == "opentelemetry.instrumentation.sqlalchemy"
               and s.context.trace_id == declare.context.trace_id for s in finished)

    declared = [r for r in logs.get_finished_logs() if "declared by" in str(r.log_record.body)]
    assert len(declared) == 1
    assert declared[0].log_record.trace_id == declare.context.trace_id
