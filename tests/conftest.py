import pytest
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.telemetry import configure_telemetry

# Must run before app.main is imported, which would otherwise install console exporters.
span_exporter = InMemorySpanExporter()
metric_reader = InMemoryMetricReader()
log_exporter = InMemoryLogRecordExporter()
configure_telemetry(
    span_processor=SimpleSpanProcessor(span_exporter),
    metric_reader=metric_reader,
    log_processor=SimpleLogRecordProcessor(log_exporter),
)


@pytest.fixture
def telemetry():
    span_exporter.clear()
    log_exporter.clear()
    return span_exporter, metric_reader, log_exporter
