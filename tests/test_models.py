"""Model adapter contract tests against a local fake endpoint (no network)."""

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from agent_core.models import (
    ModelRequestError,
    OpenAICompatibleModel,
    model_from_environment,
)


class FakeOpenAIServer:
    """Minimal OpenAI-compatible endpoint with scripted behavior per path."""

    def __init__(self, *, status=200, body=None, raw=None, delay=0.0, stream_chunks=None) -> None:
        outer = self
        self.requests: list[dict] = []

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
                outer.requests.append({
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "payload": payload,
                })
                if outer.delay:
                    import time

                    time.sleep(outer.delay)
                if outer.status != 200:
                    response = json.dumps({"error": "boom"}).encode("utf-8")
                    self.send_response(outer.status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(response)))
                    self.end_headers()
                    self.wfile.write(response)
                    return
                if payload.get("stream"):
                    self._stream_response(payload)
                    return
                if outer.raw is not None:
                    response = outer.raw
                else:
                    response = json.dumps({
                        "id": "chatcmpl-1",
                        "choices": [{
                            "index": 0,
                            "message": {"role": "assistant", "content": outer.body},
                            "finish_reason": "stop",
                        }],
                        "usage": {"prompt_tokens": 5, "completion_tokens": 3},
                    }).encode("utf-8")
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

            def _stream_response(self, payload) -> None:
                chunks = outer.stream_chunks if outer.stream_chunks is not None else ["你", "好", "呀"]
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.end_headers()
                for chunk in chunks:
                    frame = json.dumps({"choices": [{"delta": {"content": chunk}}]})
                    self.wfile.write(f"data: {frame}\n\n".encode("utf-8"))
                usage_frame = json.dumps({"choices": [{"delta": {}}], "usage": {"prompt_tokens": 2, "completion_tokens": 3}})
                self.wfile.write(f"data: {usage_frame}\n\n".encode("utf-8"))
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

            def log_message(self, *args) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.body = body if body is not None else "模型回复"
        self.status = status
        self.raw = raw
        self.delay = delay
        self.stream_chunks = stream_chunks

    def start(self) -> "FakeOpenAIServer":
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class OpenAICompatibleModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeOpenAIServer().start()
        self.addCleanup(self.fake.stop)

    def model(self, **kwargs) -> OpenAICompatibleModel:
        return OpenAICompatibleModel(self.fake.base_url, "secret-key", "test-model", **kwargs)

    def test_complete_posts_and_parses_reply(self) -> None:
        reply = self.model().complete([
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": "你好"},
        ])
        self.assertEqual(reply["content"], "模型回复")
        self.assertEqual(reply["model"], "test-model")
        self.assertEqual(reply["usage"]["prompt_tokens"], 5)
        request = self.fake.requests[0]
        self.assertEqual(request["path"], "/chat/completions")
        self.assertEqual(request["authorization"], "Bearer secret-key")
        self.assertEqual(request["payload"]["model"], "test-model")
        self.assertEqual(request["payload"]["messages"][1]["content"], "你好")

    def test_http_error_maps_to_reason_code(self) -> None:
        self.fake.status = 401
        with self.assertRaises(ModelRequestError) as caught:
            self.model().complete([{"role": "user", "content": "hi"}])
        self.assertEqual(caught.exception.reason_code, "MODEL_HTTP_ERROR")
        self.assertNotIn("secret-key", str(caught.exception))

    def test_malformed_response_maps_to_reason_code(self) -> None:
        for raw in (b"not json", b"{}", json.dumps({"choices": []}).encode("utf-8"),
                    json.dumps({"choices": [{"message": {"content": 7}}]}).encode("utf-8")):
            self.fake.raw = raw
            with self.assertRaises(ModelRequestError) as caught:
                self.model().complete([{"role": "user", "content": "hi"}])
            self.assertEqual(caught.exception.reason_code, "MODEL_INVALID_RESPONSE")

    def test_unreachable_endpoint_maps_to_reason_code(self) -> None:
        model = OpenAICompatibleModel("http://127.0.0.1:1", "key", "m", timeout_seconds=1.0)
        with self.assertRaises(ModelRequestError) as caught:
            model.complete([{"role": "user", "content": "hi"}])
        self.assertIn(
            caught.exception.reason_code,
            {"MODEL_UNREACHABLE", "MODEL_TIMEOUT", "MODEL_TRANSPORT_ERROR"},
        )

    def test_constructor_validates_arguments(self) -> None:
        with self.assertRaises(ValueError):
            OpenAICompatibleModel("ftp://bad", "key", "m")
        with self.assertRaises(ValueError):
            OpenAICompatibleModel(self.fake.base_url, "  ", "m")
        with self.assertRaises(ValueError):
            OpenAICompatibleModel(self.fake.base_url, "key", "")
        with self.assertRaises(ValueError):
            OpenAICompatibleModel(self.fake.base_url, "key", "m", timeout_seconds=0)

    def test_complete_validates_messages(self) -> None:
        model = self.model()
        for messages in ([], "not-a-list", [{"role": "user"}]):
            with self.assertRaises(ValueError):
                model.complete(messages)

    def test_complete_stream_assembles_deltas_and_invokes_callback(self) -> None:
        self.fake.stream_chunks = ["确定", "性运", "行时"]
        seen: list[str] = []
        reply = self.model().complete(
            [{"role": "user", "content": "hi"}],
        ) if False else self.model().complete_stream(
            [{"role": "user", "content": "hi"}], on_delta=seen.append,
        )
        self.assertEqual(reply["content"], "确定性运行时")
        self.assertEqual(seen, ["确定", "性运", "行时"])
        self.assertEqual(reply["usage"]["completion_tokens"], 3)
        request = self.fake.requests[0]
        self.assertIs(request["payload"]["stream"], True)

    def test_complete_stream_without_callback_still_assembles(self) -> None:
        self.fake.stream_chunks = ["a", "b"]
        reply = self.model().complete_stream([{"role": "user", "content": "hi"}])
        self.assertEqual(reply["content"], "ab")

    def test_complete_stream_http_error_maps_to_reason_code(self) -> None:
        self.fake.status = 401
        with self.assertRaises(ModelRequestError) as caught:
            self.model().complete_stream([{"role": "user", "content": "hi"}])
        self.assertEqual(caught.exception.reason_code, "MODEL_HTTP_ERROR")


class ModelFromEnvironmentTests(unittest.TestCase):
    def test_full_configuration_builds_model(self) -> None:
        model = model_from_environment({
            "AGENT_MODEL_BASE_URL": "https://api.example.com/v1",
            "AGENT_MODEL_API_KEY": "key-1",
            "AGENT_MODEL_NAME": "glm-4",
        })
        self.assertIsInstance(model, OpenAICompatibleModel)
        self.assertEqual(model.model, "glm-4")

    def test_partial_configuration_is_unconfigured(self) -> None:
        for environ in (
            {},
            {"AGENT_MODEL_BASE_URL": "https://api.example.com/v1"},
            {"AGENT_MODEL_BASE_URL": "https://x", "AGENT_MODEL_NAME": "glm-4"},
            {"AGENT_MODEL_API_KEY": "k", "AGENT_MODEL_NAME": "glm-4"},
        ):
            self.assertIsNone(model_from_environment(environ), environ)


if __name__ == "__main__":
    unittest.main()
