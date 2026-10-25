"""CXR bridge contract tests against a fake CXR-MARS service (no real model)."""

import json
import tempfile
import threading
import unittest
from email.parser import BytesParser
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from agent_core import RunStatus, Runtime
from agent_core.nodes import build_agent_from_config
from examples.cxr_bridge import CXR_REVIEW_CONFIG, build_cxr_tools


class FakeCxrService:
    """Mimics CXR-MARS ``POST /api/predict`` including the collapse gate."""

    def __init__(self, *, status=200, findings=("Cardiomegaly", "Pleural Effusion")) -> None:
        outer = self
        self.requests: list[dict] = []

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                image_bytes, form = self._parse_multipart(body)
                outer.requests.append({"path": self.path, "form": form, "image_len": len(image_bytes)})
                if not outer.status == 200:
                    response = json.dumps({"detail": "collapsed_input: input carries no signal"}).encode("utf-8")
                    self.send_response(outer.status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(response)))
                    self.end_headers()
                    self.wfile.write(response)
                    return
                response = json.dumps({
                    "class_names": ["Cardiomegaly", "Pleural Effusion", "Edema"],
                    "probs_raw": [0.82, 0.76, 0.12],
                    "pred_at_thr": list(outer.findings),
                    "threshold": 0.5,
                    "latency_ms": 3.2,
                    "collapse_margin": 0.031,
                }).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

            def _parse_multipart(self, body: bytes):
                content_type = self.headers.get("Content-Type", "")
                header = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8")
                message = BytesParser(policy=HTTP).parsebytes(header + body)
                form: dict[str, str] = {}
                image_bytes = b""
                for part in message.iter_parts():
                    name = part.get_param("name", header="content-disposition")
                    if name == "image":
                        image_bytes = part.get_payload(decode=True) or b""
                    elif name is not None:
                        form[str(name)] = part.get_payload()
                return image_bytes, form

            def log_message(self, *args) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.status = status
        self.findings = findings

    def start(self) -> "FakeCxrService":
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class CxrBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeCxrService().start()
        self.addCleanup(self.fake.stop)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.image = Path(self._tmp.name) / "study.png"
        self.image.write_bytes(b"\x89PNG fake chest x-ray bytes")

    def build_agent(self, config=None):
        tools = build_cxr_tools(self.fake.base_url, timeout_seconds=5.0)
        return build_agent_from_config(
            config or CXR_REVIEW_CONFIG, runtime=Runtime(), tools=tools,
        )

    def test_review_agent_delegates_and_reports(self) -> None:
        result = self.build_agent().run({"task": str(self.image)})
        self.assertEqual(result.status, RunStatus.COMPLETED)
        cxr = result.state["cxr"]
        self.assertEqual(cxr["finding"], "Cardiomegaly; Pleural Effusion")
        self.assertEqual(cxr["pred_at_thr"], ["Cardiomegaly", "Pleural Effusion"])
        self.assertEqual(cxr["latency_ms"], 3.2)
        self.assertEqual(result.state["summary"], "胸片分析完成,阳性发现见结果详情。")
        request = self.fake.requests[0]
        self.assertEqual(request["path"], "/api/predict")
        self.assertEqual(request["image_len"], len(self.image.read_bytes()))
        self.assertEqual(request["form"].get("threshold"), "0.5")

    def test_missing_capability_blocks(self) -> None:
        config = json.loads(json.dumps(CXR_REVIEW_CONFIG))
        config["nodes"][1]["params"].pop("capabilities")
        result = self.build_agent(config).run({"task": str(self.image)})
        self.assertEqual(result.status, RunStatus.BLOCKED)
        self.assertEqual(result.reason_code, "CAPABILITY_DENIED")
        self.assertEqual(self.fake.requests, [])

    def test_collapse_gate_surfaces_as_failure(self) -> None:
        self.fake.status = 422
        result = self.build_agent().run({"task": str(self.image)})
        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertEqual(result.reason_code, "CXR_SERVICE_REJECTED")

    def test_missing_image_fails_without_service_call(self) -> None:
        result = self.build_agent().run({"task": str(Path(self._tmp.name) / "ghost.png")})
        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertEqual(result.reason_code, "IMAGE_NOT_FOUND")
        self.assertEqual(self.fake.requests, [])

    def test_allowed_roots_whitelist(self) -> None:
        from examples.cxr_bridge import build_cxr_tools as build_with_roots
        from agent_core.nodes import build_agent_from_config

        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        secret = Path(outside.name) / "secret.png"
        secret.write_bytes(b"\x89PNG secret")

        tools = build_with_roots(self.fake.base_url, allowed_roots=(self._tmp.name,))
        agent = build_agent_from_config(CXR_REVIEW_CONFIG, runtime=Runtime(), tools=tools)
        # 白名单内 → 正常分析
        result = agent.run({"task": str(self.image)})
        self.assertEqual(result.status, RunStatus.COMPLETED)
        # 白名单外 → IMAGE_PATH_FORBIDDEN,且不触服务
        before = len(self.fake.requests)
        result = agent.run({"task": str(secret)})
        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertEqual(result.reason_code, "IMAGE_PATH_FORBIDDEN")
        self.assertEqual(len(self.fake.requests), before)


if __name__ == "__main__":
    unittest.main()
