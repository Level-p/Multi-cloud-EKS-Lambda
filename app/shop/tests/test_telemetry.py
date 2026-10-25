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

from agripulse_common import telemetry
from agripulse_common.cache import Cache
from agripulse_common.testing import token
from app import main as shop
from test_shop import ADDRESS, BOLA, FakeFarm


def test_off_without_an_endpoint(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    assert telemetry.setup(shop.app) is None


@pytest.fixture
def otel(monkeypatch):
    monkeypatch.setenv("OTEL_SERVICE_NAME", "shop")
    # In the pod setup() runs at import, before the app serves anything; other tests may have started it already
    shop.app.middleware_stack = None
    spans, logs = InMemorySpanExporter(), InMemoryLogRecordExporter()
    t = telemetry.setup(shop.app, loggers=("agripulse",), redis=True, span_exporter=spans, log_exporter=logs)
    shop._sessions = None                                   # created after instrumenting: its SQL is traced
    shop._farm = FakeFarm()
    shop._cache = Cache(fakeredis.FakeRedis(decode_responses=True))
    yield t, spans, logs
    FastAPIInstrumentor.uninstrument_app(shop.app)
    shop.app.middleware_stack = None
    EngineTracer.remove_all_event_listeners()
    telemetry._trace_engine = None
    RedisInstrumentor().uninstrument()
    logging.getLogger("agripulse").removeHandler(t.handler)
    shop._sessions, shop._cache, shop._farm = None, None, None


def test_an_order_is_one_trace_with_sql_cache_and_logs(otel, caplog):
    t, spans, logs = otel
    caplog.set_level(logging.INFO, logger="agripulse")   # in the pod logging.basicConfig sets INFO; pytest pre-empts it
    client = TestClient(shop.app)
    auth = {"Authorization": f"Bearer {token('buyer', user_id=BOLA)}"}
    assert client.get("/api/catalog").status_code == 200   # cache miss: Redis GET, then SET
    placed = client.post("/api/orders", json={"items": [{"listing_id": 1, "quantity": 1}], **ADDRESS}, headers=auth)
    assert placed.status_code == 201, placed.text
    assert client.get("/healthz").status_code == 200       # probes are not traced
    t.tracer_provider.force_flush()
    t.logger_provider.force_flush()

    finished = spans.get_finished_spans()
    servers = {s.attributes.get("http.route"): s for s in finished if s.kind == SpanKind.SERVER}
    assert set(servers) == {"/api/catalog", "/api/orders"}
    assert servers["/api/orders"].resource.attributes["service.name"] == "shop"

    def scopes(route):
        trace = servers[route].context.trace_id
        return {s.instrumentation_scope.name for s in finished if s.context.trace_id == trace}
    assert "opentelemetry.instrumentation.redis" in scopes("/api/catalog")
    assert "opentelemetry.instrumentation.sqlalchemy" in scopes("/api/orders")

    placed_logs = [r for r in logs.get_finished_logs() if "order(s) placed" in str(r.log_record.body)]
    assert len(placed_logs) == 1
    assert placed_logs[0].log_record.trace_id == servers["/api/orders"].context.trace_id
