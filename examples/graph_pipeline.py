"""Minimal v0.1 Graph Runtime example with a controlled route."""

from agent_core import Graph, NodeResult, Runtime


def build_graph():
    def validate(ctx):
        text = ctx.input.get("text", "")
        if not isinstance(text, str) or not text.strip():
            return NodeResult.blocked("EMPTY_TEXT")
        return NodeResult.success({"normalized": text.strip()}, route="valid")

    def finish(ctx):
        return NodeResult.success({"message": f"Accepted: {ctx.state['normalized']}"})

    return (
        Graph()
        .add_node("validate", validate)
        .add_node("finish", finish)
        .set_entry("validate")
        .add_edge("validate", "finish", when="valid")
        .compile()
    )


if __name__ == "__main__":
    for text in ("hello", "   "):
        result = Runtime().run(build_graph(), {"text": text})
        print(result.status.value, result.state, result.reason_code)
