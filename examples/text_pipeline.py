"""Example domain-neutral application: a text processing pipeline.

Proves agent_core carries no inspection vocabulary: the same runtime that
runs the wafer workflow runs this one. Steps: normalize → validate →
analyze → classify. An empty text is BLOCKED with a reason code instead of
producing a meaningless classification — the same fail-closed decision
boundary the inspection workflow uses.
"""

from __future__ import annotations

from pathlib import Path

from agent_core import RunStore, Step, StepOutcome, StepStatus, Tool, ToolRegistry, WorkflowRunner, tool_step


def build_text_pipeline(store: RunStore | None = None) -> WorkflowRunner:
    """Build the example text pipeline; no inspection vocabulary anywhere."""

    registry = ToolRegistry()
    registry.register(Tool(
        "normalize", lambda args: {"text": " ".join(args["text"].split())}
    ))
    registry.register(Tool(
        "analyze", lambda args: {
            "char_count": len(args["text"]),
            "word_count": len(args["text"].split()),
        }
    ))
    registry.register(Tool(
        "classify", lambda args: {
            "length_class": (
                "short" if args["word_count"] < 5
                else "medium" if args["word_count"] < 20
                else "long"
            ),
        }
    ))

    def validate(context) -> StepOutcome:
        text = context.output_of("normalize")["text"]
        if not text:
            return StepOutcome(StepStatus.BLOCKED, reason_code="EMPTY_TEXT")
        return StepOutcome(StepStatus.COMPLETED, {"non_empty": True})

    return WorkflowRunner([
        tool_step("normalize", "normalize", registry,
                  lambda context: {"text": context.input["text"]}),
        Step("validate", validate),
        tool_step("analyze", "analyze", registry,
                  lambda context: {"text": context.output_of("normalize")["text"]}),
        tool_step("classify", "classify", registry,
                  lambda context: context.output_of("analyze")),
    ], store=store)


def _format_result(result) -> str:
    lines = [f"run {result.run_id}: {result.status.value}"
             + (f" (stopped at {result.stopped_at}, {result.reason_code})"
                if result.stopped_at else "")]
    for event in result.trace:
        line = f"  {event.step:<10} {event.status:<10} {event.duration_ms:>7.3f}ms"
        if event.reason_code:
            line += f"  [{event.reason_code}]"
        lines.append(line)
    if result.status.value == "COMPLETED":
        stats = result.outputs["analyze"]
        lines.append(f"  → {stats['word_count']} words, {stats['char_count']} chars, "
                     f"class={result.outputs['classify']['length_class']}")
    return "\n".join(lines)


def main() -> None:
    store = RunStore(Path("outputs") / "examples" / "text_runs")
    pipeline = build_text_pipeline(store=store)
    samples = [
        ("normal", "  Hello   world,  this is the Hairline example pipeline.  "),
        ("empty", "   "),
        ("long", "word " * 30),
    ]
    for name, text in samples:
        result = pipeline.run({"text": text}, run_id=f"example-{name}")
        print(_format_result(result))
        print()


if __name__ == "__main__":
    main()
