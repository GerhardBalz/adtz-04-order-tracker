import logging
import os
import time

from opentelemetry import _logs, metrics, trace
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.logging.handler import LoggingHandler
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor, ConsoleLogRecordExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import ConsoleMetricExporter, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.trace import SpanKind, Status, StatusCode
from starlette.requests import Request


SERVICE_NAME = "order-tracker"
_configured = False


def default_exporters():
    """Return (span, metric, log) exporters.

    With OTEL_EXPORTER_OTLP_ENDPOINT set (as in Compose), telemetry goes to the
    Collector over OTLP/HTTP; otherwise it is printed to the console.
    """
    if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return OTLPSpanExporter(), OTLPMetricExporter(), OTLPLogExporter()
    return ConsoleSpanExporter(), ConsoleMetricExporter(), ConsoleLogRecordExporter()


def configure_telemetry(span_processor=None, metric_reader=None, log_processor=None):
    """Install global providers that export traces, metrics and logs.

    Runs once per process; tests call it first with in-memory exporters.
    """
    global _configured
    if _configured:
        return
    _configured = True
    resource = Resource.create({"service.name": SERVICE_NAME})
    span_exporter, metric_exporter, log_exporter = default_exporters()

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(span_processor or BatchSpanProcessor(span_exporter))
    trace.set_tracer_provider(tracer_provider)

    # Export interval defaults to 60s; override with OTEL_METRIC_EXPORT_INTERVAL (ms).
    reader = metric_reader or PeriodicExportingMetricReader(metric_exporter)
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[reader]))

    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(
        log_processor or BatchLogRecordProcessor(log_exporter)
    )
    _logs.set_logger_provider(logger_provider)
    app_logger = logging.getLogger("app")
    app_logger.setLevel(logging.INFO)
    app_logger.addHandler(LoggingHandler(logger_provider=logger_provider))


def flush_telemetry():
    for provider in (trace.get_tracer_provider(), metrics.get_meter_provider(),
                     _logs.get_logger_provider()):
        if hasattr(provider, "force_flush"):
            provider.force_flush()


tracer = trace.get_tracer(__name__)
meter = metrics.get_meter(__name__)
request_duration = meter.create_histogram(
    "http.server.request.duration",
    unit="s",
    description="Duration of HTTP server requests",
)


async def telemetry_middleware(request: Request, call_next):
    """Wrap each request in a server span and record its duration, route and status code."""
    method = request.method
    start = time.perf_counter()
    with tracer.start_as_current_span(
        method, kind=SpanKind.SERVER, record_exception=False, set_status_on_exception=False
    ) as span:
        try:
            response = await call_next(request)
            status_code = response.status_code
        except Exception as exc:
            # Unhandled errors become a 500 in Starlette's outer error middleware.
            status_code = 500
            span.record_exception(exc)
            raise
        finally:
            # The route template is only known after routing; unmatched paths share one
            # value so raw URLs (and order IDs) never become metric attributes.
            route_obj = request.scope.get("route")
            route = getattr(route_obj, "path", None) or "unmatched"
            span.update_name(f"{method} {route}")
            span.set_attributes({
                "http.request.method": method,
                "http.route": route,
                "url.path": request.url.path,
                "http.response.status_code": status_code,
            })
            if status_code >= 500:
                span.set_status(Status(StatusCode.ERROR))
            request_duration.record(time.perf_counter() - start, {
                "http.request.method": method,
                "http.route": route,
                "http.response.status_code": status_code,
            })
    return response
