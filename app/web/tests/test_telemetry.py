"""OpenTelemetry: a request becomes one trace (HTTP, SQL and cache spans) and its log lines carry the trace id."""
import logging

import fakeredis
import pytest
from fastapi.testclient import TestClient
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.instrumentation.sqlalchemy.engine import EngineTracer
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind

import app.main as web
from app import telemetry
from app.cache import Cache
from app.db import make_session_factory

USER = {"X-Auth-Request-Email": "agronomist@coop.example"}


def test_off_without_an_endpoint(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    assert telemetry.setup(web.app) is None


@pytest.fixture
def otel(monkeypatch):
    monkeypatch.setenv("CSRF_SECRET", "test-secret")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "agripulse-web")
    # In the pod setup() runs at import, before the app serves anything; other tests may have started it already
    web.app.middleware_stack = None
    spans, logs = InMemorySpanExporter(), InMemoryLogRecordExporter()
    t = telemetry.setup(web.app, loggers=("agripulse",), redis=True, span_exporter=spans, log_exporter=logs)
    web._sessions = make_session_factory("sqlite://")     # created after instrumenting: its SQL is traced
    web._cache = Cache(fakeredis.FakeRedis(decode_responses=True))
    yield t, spans, logs
    FastAPIInstrumentor.uninstrument_app(web.app)
    web.app.middleware_stack = None
    EngineTracer.remove_all_event_listeners()
    telemetry._trace_engine = None
    RedisInstrumentor().uninstrument()
    logging.getLogger("agripulse").removeHandler(t.handler)
    web._sessions, web._cache = None, None


def test_request_is_one_trace_with_sql_cache_and_logs(otel, caplog):
    t, spans, logs = otel
    caplog.set_level(logging.INFO, logger="agripulse")   # in the pod logging.basicConfig sets INFO; pytest pre-empts it
    client = TestClient(web.app)
    r = client.post("/farms", data={"csrf": web.csrf_token("agronomist@coop.example"), "name": "Trace farm",
                                    "region": "Oyo", "hectares": "5"}, headers=USER, follow_redirects=False)
    assert r.status_code == 303, r.text
    farm = r.headers["location"]
    assert client.get(farm, headers=USER).status_code == 200      # cache miss: Redis GET, SQL, Redis SET
    assert client.get("/healthz").status_code == 200              # probes are not traced
    t.tracer_provider.force_flush()
    t.logger_provider.force_flush()

    finished = spans.get_finished_spans()
    servers = {s.attributes.get("http.route"): s for s in finished if s.kind == SpanKind.SERVER}
    assert set(servers) == {"/farms", "/farms/{farm_id}"}
    assert servers["/farms"].resource.attributes["service.name"] == "agripulse-web"

    read = servers["/farms/{farm_id}"]
    scopes = {s.instrumentation_scope.name for s in finished if s.context.trace_id == read.context.trace_id}
    assert "opentelemetry.instrumentation.sqlalchemy" in scopes
    assert "opentelemetry.instrumentation.redis" in scopes

    created = [r for r in logs.get_finished_logs() if "farm created" in str(r.log_record.body)]
    assert len(created) == 1
    assert created[0].log_record.trace_id == servers["/farms"].context.trace_id
