"""Bridge a CXR-MARS vision service into the agent runtime as a Tool.

CXR-MARS(https://github.com/AAA-Allen 智源项目,本机 ``D:/AAA-My-project/CXR-MARS``)
是胸片多标签分类系统:图像进、结构化概率出。它**不是** ModelAdapter(那是
对话形状的契约:messages 进、content 字符串出),而是**领域工具**——正确的
接入位置是 Tool 边界:结构化参数进、结构化 JSON 出、能力显式授予、fail-closed。

本模块提供:

- ``build_cxr_tools(base_url)``:注册 ``cxr_analyze`` 工具(要求
  ``cxr_inference`` 能力),纯标准库实现 multipart 上传,调用 CXR-MARS
  的 ``POST /api/predict`` 并整理输出(阳性标签、逐类概率、塌陷余量、延迟)。
- ``CXR_REVIEW_CONFIG``:声明式审阅 Agent——校验图像路径 → 委派工具 →
  汇总;配置里显式声明授予 ``cxr_inference`` 能力。
- ``main()``:组装一个挂载了该工具与 Agent 的完整 HTTP 服务。

先启动 CXR-MARS 服务(在 CXR-MARS 仓库内)::

    python scripts/serve_agent.py            # 默认 127.0.0.1:8300

再启动本桥接::

    python -m examples.cxr_bridge            # 默认 127.0.0.1:8766

CXR-MARS 的 422 ``collapsed_input`` 门控(输入未携带有效图像信息)会作为
BLOCKED 原因码原样透传,不伪装成业务成功。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from agent_core import Tool, ToolRegistry
from agent_core.nodes import build_agent_from_config

CXR_REVIEW_CONFIG = {
    "name": "cxr-reviewer",
    "instructions": "接收胸片路径,调用 CXR-MARS 分析,汇总阳性发现。",
    "description": "Delegates chest X-ray analysis to the CXR-MARS service (requires cxr_inference).",
    "metadata": {"example": True, "domain": "medical-imaging"},
    "entry": "intake",
    "nodes": [
        {"id": "intake", "type": "validate_input",
         "params": {"field": "image_path", "blocked_reason": "EMPTY_IMAGE_PATH", "route": "analyze"}},
        {"id": "analyze", "type": "tool",
         "params": {
             "name": "cxr_analyze",
             "capabilities": ["cxr_inference"],
             "arguments": {"image_path": "{image_path}"},
             "output_key": "cxr",
             "route": "report",
         }},
        {"id": "report", "type": "template",
         "params": {"updates": {"summary": "胸片分析完成,阳性发现见结果详情。"}}},
    ],
    "edges": [
        {"source": "intake", "target": "analyze", "when": "analyze"},
        {"source": "analyze", "target": "report", "when": "report"},
    ],
}


class CxrServiceError(Exception):
    """CXR-MARS 调用失败;reason_code 会成为节点的 BLOCKED/FAILED 原因码."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def _multipart(fields: Mapping[str, str], file_field: str, file_bytes: bytes, filename: str) -> tuple[bytes, str]:
    boundary = uuid4().hex
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode("utf-8")
        )
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; filename=\"{filename}\"\r\n"
        "Content-Type: application/octet-stream\r\n\r\n".encode("utf-8") + file_bytes + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), boundary


def invoke_cxr_predict(base_url: str, image_path: str, *, timeout_seconds: float = 60.0) -> dict[str, Any]:
    path = Path(image_path)
    if not path.is_file():
        raise CxrServiceError("IMAGE_NOT_FOUND", f"image file not found: {image_path}")
    try:
        image_bytes = path.read_bytes()
    except OSError as exc:
        raise CxrServiceError("IMAGE_UNREADABLE", f"cannot read image: {type(exc).__name__}") from None
    body, boundary = _multipart({"threshold": "0.5"}, "image", image_bytes, path.name)
    request = Request(
        base_url.rstrip("/") + "/api/predict",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("detail", "")
        except Exception:
            pass
        code = "CXR_SERVICE_REJECTED" if exc.code == 422 else "CXR_SERVICE_ERROR"
        raise CxrServiceError(code, f"CXR-MARS returned HTTP {exc.code}: {detail}") from None
    except URLError as exc:
        raise CxrServiceError("CXR_SERVICE_UNREACHABLE", f"CXR-MARS unreachable: {type(getattr(exc, 'reason', None)).__name__}") from None
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise CxrServiceError("CXR_SERVICE_INVALID_RESPONSE", "CXR-MARS response is not valid JSON") from None

    findings = payload.get("pred_at_thr")
    if not isinstance(findings, list):
        raise CxrServiceError("CXR_SERVICE_INVALID_RESPONSE", "CXR-MARS response has no pred_at_thr")
    finding_text = "; ".join(str(item) for item in findings) or "无阳性发现(阈值内)"
    return {
        "finding": finding_text,
        "pred_at_thr": findings,
        "probs_raw": payload.get("probs_raw"),
        "class_names": payload.get("class_names"),
        "collapse_margin": payload.get("collapse_margin"),
        "latency_ms": payload.get("latency_ms"),
        "service": base_url,
    }


def build_cxr_tools(base_url: str, *, timeout_seconds: float = 60.0) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(Tool(
        "cxr_analyze",
        lambda args: invoke_cxr_predict(base_url, args["image_path"], timeout_seconds=timeout_seconds),
        required_capabilities=frozenset({"cxr_inference"}),
    ))
    return registry


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Serve a cxr-reviewer agent backed by CXR-MARS.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--cxr-base-url", default="http://127.0.0.1:8300")
    parser.add_argument("--store-dir", default="outputs/cxr_runs")
    parser.add_argument("--frontend-dir", default=None)
    args = parser.parse_args()

    from agent_core import Runtime
    from agent_core.persistence import RunStore
    from agent_core.server import create_server, register_agents_from_config

    tools = build_cxr_tools(args.cxr_base_url)
    runtime = Runtime(store=RunStore(args.store_dir))
    server = create_server(
        host=args.host, port=args.port, store_dir=args.store_dir,
        frontend_dir=args.frontend_dir, runtime=runtime,
    )
    register_agents_from_config(
        CXR_REVIEW_CONFIG, server.registry, runtime=runtime, tools=tools,
    )
    import logging

    logging.basicConfig(level=logging.INFO)
    print(f"cxr-reviewer ready on http://{args.host}:{args.port} (CXR-MARS: {args.cxr_base_url})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
