"""Arena adapter: common boundary between HTTP and the agent loop.

Ordinary Arena requests have no chat memory. The adapter:
  1. Deep-copies the request to avoid mutation leaks
  2. Clamps max_steps to the system ceiling
  3. Wraps execution in a timeout (category E: budget)
  4. Catches ALL exceptions and always returns a valid ArenaResponse
  5. Caps response size to 50KB
"""

import asyncio
import logging
import traceback
from time import perf_counter

from app.agent import run_agent
from app.config import settings
from app.models import ArenaResponse

log = logging.getLogger("arena")


async def execute(request, history=None, model="unconfigured"):
    """Run the agent and guarantee a valid ArenaResponse no matter what.

    This is the function that /arena/run and /chat both call.
    It must NEVER raise - the spec requires a valid response schema even on exceptions.
    """
    started = perf_counter()

    # Deep-copy to avoid mutating the original request
    request = request.model_copy(deep=True)

    # System ceiling: arena_config can only lower limits, not raise them
    request.arena_config.max_steps = min(
        request.arena_config.max_steps, settings.max_steps
    )

    try:
        # Timeout wraps the entire agent loop (category E: budget)
        async with asyncio.timeout(settings.run_timeout_seconds):
            result = await run_agent(request, history or [], model)

            # Validate the result is a proper ArenaResponse
            if isinstance(result, dict):
                result = ArenaResponse.model_validate(result)
            elif isinstance(result, ArenaResponse):
                # Re-validate to catch any schema drift
                result = ArenaResponse.model_validate(result.model_dump())

    except TimeoutError:
        # Category E: execution budget exceeded (timeout)
        log.warning("Run timed out for request %s", request.request_id)
        result = ArenaResponse(
            request_id=request.request_id,
            status="budget_exceeded",
            final_response="The run timed out before completion.",
            stop_reason="time_budget_reached",
            events=[{"step": 0, "event": "timeout",
                     "reason": "run_timeout_seconds exceeded"}],
        )

    except Exception as exc:
        # Catch-all: the spec requires we NEVER return a non-schema response.
        log.error("Internal error for request %s: %s\n%s",
                 request.request_id, exc, traceback.format_exc())
        result = ArenaResponse(
            request_id=request.request_id,
            status="failed",
            final_response="The run stopped due to an internal error.",
            stop_reason="internal_error",
            errors=[{"type": "internal_error",
                     "message": str(exc)[:500],
                     "step": 0}],
        )

    # Record total latency
    result.metrics.latency_ms = round((perf_counter() - started) * 1000, 1)

    # Cap response size to 50KB (spec limit)
    response_bytes = len(result.model_dump_json().encode())
    if response_bytes > 50000:
        log.warning("Response too large (%d bytes), truncating", response_bytes)
        result = ArenaResponse(
            request_id=request.request_id,
            status="failed",
            final_response="Response exceeded the size limit.",
            stop_reason="response_too_large",
        )

    log.info("request=%s status=%s steps=%s latency_ms=%.0f",
            request.request_id, result.status, result.steps,
            result.metrics.latency_ms)

    return result
