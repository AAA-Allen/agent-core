"""Bundled deterministic workbench demo agent."""

from __future__ import annotations

import re
from typing import Any

from .registry import AgentRegistry, default_tools
from .runtime import Runtime


def _build_workbench(runtime: Runtime, registry: Any, memory: Any = None) -> Agent:
    """Deterministic demo agent backing the bundled frontend workbench.

    It decomposes the requested task text into plan entries, runs a registered
    text-stats tool over the task, and produces a summary. With session memory
    bound it also records processed tasks and surfaces the session position.
    No model or network access is involved; the point is to exercise the real
    runtime boundaries (graph execution, tool gating, trace and lifecycle
    persistence).
    """
    from .agent import AgentBuilder
    from .results import NodeResult

    def intake(ctx):
        task = ctx.input.get("task")
        if not isinstance(task, str) or not task.strip():
            return NodeResult.blocked("EMPTY_TASK")
        return NodeResult.success({"task": task.strip()}, route="planned")

    def plan(ctx):
        task = ctx.state["task"]
        pieces = [piece.strip() for piece in re.split(r"[。;；!！?\n]+", task) if piece.strip()]
        entries = pieces or [task]
        plan_items = [{"index": index + 1, "title": entry[:60]} for index, entry in enumerate(entries)]
        if ctx.memory is not None:
            ctx.memory.write("last_task", task)
        return NodeResult.success(
            {"plan": plan_items, "step_count": len(plan_items)}, route="executed"
        )

    def execute(ctx):
        stats = ctx.tools.invoke("text_stats", {"task": ctx.state["task"]})
        if stats.status.value != "COMPLETED":
            return NodeResult.failed("TOOL_EXECUTION_FAILED")
        processed = [item["title"] for item in ctx.state["plan"]]
        if ctx.memory is not None:
            history = ctx.memory.read("processed_tasks", [])
            if not isinstance(history, list):
                history = []
            history.append({"task": ctx.state["task"], "steps": ctx.state["step_count"]})
            ctx.memory.write("processed_tasks", history)
        return NodeResult.success(
            {"text_stats": dict(stats.output), "processed": processed}, route="summarized"
        )

    def summarize(ctx):
        task = ctx.state["task"]
        count = ctx.state["step_count"]
        summary = f"已完成对“{task}”的 {count} 个步骤的确定性处理。"
        updates: dict[str, Any] = {"summary": summary}
        if ctx.memory is not None:
            history = ctx.memory.read("processed_tasks", [])
            session_count = len(history) if isinstance(history, list) else 0
            updates["session_task_count"] = session_count
            updates["summary"] = f"{summary}(本会话第 {session_count} 个任务)"
        return NodeResult.success(updates)

    return (
        AgentBuilder(
            "workbench",
            instructions="接收任务文本,拆解为计划步骤,执行统计并输出摘要。",
            description="Deterministic workbench demo agent.",
            tools=registry,
            memory=memory,
        )
        .add_node("intake", intake)
        .add_node("plan", plan)
        .add_node("execute", execute)
        .add_node("summarize", summarize)
        .set_entry("intake")
        .add_edge("intake", "plan", when="planned")
        .add_edge("plan", "execute", when="executed")
        .add_edge("execute", "summarize", when="summarized")
        .build(runtime=runtime)
    )


def build_default_registry(*, runtime: Runtime) -> AgentRegistry:
    """Registry with the bundled demo agent; extend it via ``register``."""
    registry = AgentRegistry()
    tools = default_tools()
    registry.register_factory(
        "workbench",
        lambda memory=None: _build_workbench(runtime, tools, memory),
    )
    return registry
