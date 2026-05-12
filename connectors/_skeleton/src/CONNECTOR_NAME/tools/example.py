"""Skeleton MCP tool. Copy and rename per concrete tool.

Conventions:
- Tool function name is always `run`.
- Inputs and outputs are explicit Pydantic models (no **kwargs).
- Read `trace_id` from the MCP context and include it in every log.
- Bodies are only logged at `debug` level (oscar_logging handles the gate).
"""

from pydantic import BaseModel, Field

from oscar_logging import log


class ExampleInput(BaseModel):
    query: str = Field(..., description="The thing the caller is asking about")


class ExampleOutput(BaseModel):
    result: str
    fetched_at: str


async def run(input: ExampleInput, ctx) -> ExampleOutput:
    trace_id = ctx.request_context.meta.get("trace_id") if hasattr(ctx, "request_context") else None
    log.info("connector.call", event_type="example", trace_id=trace_id, query=input.query)
    log.debug("connector.call.body", trace_id=trace_id, input=input.model_dump())
    # ...do the external call here...
    return ExampleOutput(result="replace me", fetched_at="1970-01-01T00:00:00Z")
