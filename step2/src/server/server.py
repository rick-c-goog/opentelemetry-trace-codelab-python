# Copyright 2021 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import re
from concurrent import futures

import grpc
import structlog
from google.cloud import storage
from grpc_health.v1 import health_pb2, health_pb2_grpc

from opentelemetry import propagate, trace
from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.propagators.cloud_trace_propagator import CloudTraceFormatPropagator
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

RequestsInstrumentor().instrument()

import shakesapp_pb2
import shakesapp_pb2_grpc

BUCKET_NAME = "dataflow-samples"
BUCKET_PREFIX = "shakespeare/"


# Structured log configuration
def field_name_modifier(_, __, event_dict):
    """Replace log level field name 'level' with 'serverity' to meet
    Cloud Logging's data model.
    Make sure to call this processor after structlog.stdlib.add_log_level.
    https://cloud.google.com/logging/docs/reference/v2/rpc/google.logging.v2?hl=en#google.logging.v2.LogEntry
    """
    event_dict["severity"] = event_dict["level"]
    del event_dict["level"]
    return event_dict


def get_json_logger():
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            field_name_modifier,
            structlog.processors.TimeStamper("iso"),
            structlog.processors.JSONRenderer(),
        ]
    )
    return structlog.get_logger()


logger = get_json_logger()
exporter = CloudTraceSpanExporter()
trace.set_tracer_provider(TracerProvider())
trace.get_tracer_provider().add_span_processor(
        SimpleSpanProcessor(exporter)
)
propagate.set_global_textmap(CloudTraceFormatPropagator())

class ShakesappService(shakesapp_pb2_grpc.ShakespeareServiceServicer):
    """ShakesappService accepts request from the clients and search query
    string from Shakespare works fetched from GCS.
    """

    def __init__(self):
        super().__init__()

    def GetMatchCount(self, request, context):
        logger.info(f"query: {request.query}")
        tracer = trace.get_tracer(__name__)
        texts = read_files_multi()
        count = 0

        query = request.query.lower()
        # TODO: intentionally implemented in inefficient way.
        with tracer.start_as_current_span("matchcount") as root_span:
          root_span.add_event(name="count_start")
          for text in texts:
            lines = text.split("\n")
            for line in lines:
                line = line.lower()
                matched = re.search(query, line)
                if matched is not None:
                    count += 1
          root_span.add_event(name="count_end")
        logger.info(f"query '{query}' matched count: {count}")
        return shakesapp_pb2.ShakespeareResponse(match_count=count)

    def Check(self, request, context):
        return health_pb2.HealthCheckResponse(
            status=health_pb2.HealthCheckResponse.SERVING
        )

    def Watch(self, request, context):
        return health_pb2.HealthCheckResponse(
            status=health_pb2.HealthCheckResponse.UNIMPLEMENTED
        )


def read_files_multi():
    """read_files_multi fetchse Shakespeare works from GCS in multi threads.

    TODO: This part should be multiprocess.
    """
    client = storage.Client()
    bucket = client.get_bucket(BUCKET_NAME)
    itr = client.list_blobs(bucket, prefix=BUCKET_PREFIX)
    blobs = list(itr)

    executor = futures.ThreadPoolExecutor(max_workers=8)
    results = []
    for blob in blobs:
        ret = executor.submit(blob.download_as_bytes)
        results.append(ret)
    executor.shutdown()
    logger.info(f"number of files: {len(results)}")
    return [r.result().decode("utf-8") for r in results]


def serve():
    # Add gRPC services to server
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    service = ShakesappService()
    shakesapp_pb2_grpc.add_ShakespeareServiceServicer_to_server(service, server)
    health_pb2_grpc.add_HealthServicer_to_server(service, server)

    # Start gRCP server
    port = os.environ.get("PORT", "5050")
    addr = f"0.0.0.0:{port}"
    logger.info(f"starting server: {addr}")
    server.add_insecure_port(addr)
    server.start()
    server.wait_for_termination()


if __name__ == "__main__":
    serve()
