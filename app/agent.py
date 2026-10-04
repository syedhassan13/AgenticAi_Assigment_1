"""The agent loop: observe -> model decision -> validate -> execute -> repeat.

Key design decisions (see inline comments):
- The MODEL chooses the next action; the SYSTEM owns stopping.
- Each model call (including repair retries) counts against max_steps.
- Validation is two-stage: schema (Pydantic) + semantic (business rules).
- Max 2 repair retries per decision, then contract_error.
- Tool execution goes through a single gateway with fault injection.
- Arena fault injection is honored via arena_config.fault.

Six stress categories handled by GENERAL MECHANISMS (not phrase matching):
  A: Ambiguity     - model trained to ask clarification; validation rejects invented data
  B: Injection     - external content delimited + system prompt hierarchy enforced
  C: Contract      - Pydantic schema + semantic validation + bounded repair
  D: Tool failure  - fault injection + bounded retry in execute_tool gateway
  E: Budget        - step counter + timeout in arena.execute; explicit budget_exceeded
  F: Autonomy      - BLOCKED_ACTIONS allow-list; model instructed to refuse
"""

from __future__ import annotations
import logging
from time import perf_counter

from app.config import settings
from app.llm import call_llm
from app.models import ArenaResponse, AgentState, ToolTrace, Metrics
from app.prompts import assemble_messages
from app.tools import execute_tool, BLOCKED_ACTIONS
from app.validation import (
    parse_decision, validate_semantics, build_repair_feedback,
    TOOL_ALLOW_LIST,
)

log = logging.getLogger("agent")


async def run_agent(request, history: list, model: str) -> ArenaResponse:
    """Main agent loop.  Returns an ArenaResponse (always valid, even on error).

    Flow:
      1. Init state with arena_config limits
      2. LOOP while budget remains:
         a. Assemble messages (all 5 context layers)
         b. Call LLM
         c. Parse + validate decision (with bounded repair)
         d. Execute tool OR stop
         e. Feed observation back
      3. Build and return ArenaResponse
    """
    # --- 1. Initialise state ---
    max_steps = min(request.arena_config.max_steps, settings.max_steps)
    state = AgentState(
        max_steps=max_steps,
        max_retries=settings.max_repair_retries,
    )
    state.record_event("agent_start", model=model)

    # Extract fault config from arena request (for category D injection)
    fault_config = request.arena_config.fault

    # Handle invalid_agent_decision fault: if the arena wants to test contract
    # recovery (category C), we simulate a bad first LLM response.
    inject_bad_decision = False
    fault_type = getattr(fault_config, "type", "none")
    if fault_type == "invalid_agent_decision":
        inject_bad_decision = True

    # The user's task text - this is the USER context layer
    task = request.task

    # --- 2. Agent loop ---
    # The model decides what to do; the system decides when to stop.
    while state.budget_remaining():
        state.step += 1
        step_start = perf_counter()

        # --- 2a. Assemble prompt (all context layers) ---
        repair_feedback = None
        decision = None

        # Bounded repair loop: up to max_retries attempts on validation failure
        for repair_attempt in range(1 + state.max_retries):
            # Build messages with current state + any repair feedback
            messages = assemble_messages(
                task=task,
                state=state,
                history=history,
                external_context=request.external_context,
                observations=state.observations,
                repair_feedback=repair_feedback,
            )

            # --- 2b. Call LLM ---
            try:
                llm_response = await call_llm(
                    messages=messages,
                    model=model,
                    max_tokens=settings.max_output_tokens,
                )
                state.model_calls += 1
                state.input_tokens += llm_response.input_tokens
                state.output_tokens += llm_response.output_tokens

                raw_text = llm_response.text

            except Exception as exc:
                # LLM call itself failed - this is a dependency failure (cat D)
                log.error("LLM call failed at step %d: %s", state.step, exc)
                state.record_error("llm_error", str(exc))
                state.status = "failed"
                state.stop_reason = "llm_call_failed"
                state.record_event("agent_stop", reason=state.stop_reason)
                return _build_response(request, state)

            # --- Category C fault: inject a malformed decision on first call ---
            if inject_bad_decision and not getattr(state, "_bad_decision_fired", False):
                state._bad_decision_fired = True
                raw_text = '{"status": "completed", "action": 999, "arguments": "not_a_dict"}'
                log.warning("FAULT INJECTION: invalid_agent_decision")

            # --- 2c. Parse and validate the decision ---
            try:
                decision = parse_decision(raw_text)
            except ValueError as exc:
                # Schema validation failed - try repair (category C mechanism)
                log.warning("Schema parse failed (attempt %d): %s",
                           repair_attempt + 1, exc)
                state.record_error("schema_error", str(exc))

                if repair_attempt < state.max_retries:
                    repair_feedback = build_repair_feedback(raw_text, [str(exc)])
                    # Repair costs a step
                    if not state.budget_remaining():
                        state.status = "contract_error"
                        state.stop_reason = "repair_budget_exceeded"
                        state.record_event("agent_stop", reason=state.stop_reason)
                        return _build_response(request, state)
                    state.step += 1
                    continue
                else:
                    # Max retries exhausted - typed contract_error (spec section 9)
                    state.status = "contract_error"
                    state.stop_reason = "schema_validation_failed_after_retries"
                    state.record_event("agent_stop", reason=state.stop_reason)
                    return _build_response(request, state)

            # Schema passed - now semantic validation
            semantic_issues = validate_semantics(decision, state)
            if semantic_issues:
                log.warning("Semantic issues (attempt %d): %s",
                           repair_attempt + 1, semantic_issues)
                state.record_error("semantic_error", "; ".join(semantic_issues))

                if repair_attempt < state.max_retries:
                    repair_feedback = build_repair_feedback(raw_text, semantic_issues)
                    if not state.budget_remaining():
                        state.status = "contract_error"
                        state.stop_reason = "repair_budget_exceeded"
                        state.record_event("agent_stop", reason=state.stop_reason)
                        return _build_response(request, state)
                    state.step += 1
                    continue
                else:
                    state.status = "contract_error"
                    state.stop_reason = "semantic_validation_failed_after_retries"
                    state.record_event("agent_stop", reason=state.stop_reason)
                    return _build_response(request, state)

            # Validation passed - break out of repair loop
            break

        if decision is None:
            # Safety net
            state.status = "contract_error"
            state.stop_reason = "no_valid_decision"
            state.record_event("agent_stop", reason=state.stop_reason)
            return _build_response(request, state)

        # --- 2d. Act on the decision ---

        # Terminal statuses - the model chose to stop
        if decision.status == "needs_clarification":
            # Category A: ambiguity handled correctly
            state.status = "needs_clarification"
            state.stop_reason = "clarification_needed"
            state.record_event("agent_stop", reason=state.stop_reason,
                              message=decision.user_message)
            return _build_response(request, state, decision.user_message)

        if decision.status == "completed":
            state.status = "completed"
            state.stop_reason = "goal_completed"
            state.record_event("agent_stop", reason=state.stop_reason)
            return _build_response(request, state, decision.user_message)

        if decision.status == "blocked":
            # Category F: autonomy boundary respected
            state.status = "blocked"
            state.stop_reason = "autonomy_boundary"
            state.record_event("agent_stop", reason=state.stop_reason,
                              message=decision.user_message)
            return _build_response(request, state, decision.user_message)

        if decision.status == "failed":
            state.status = "failed"
            state.stop_reason = "agent_declared_failure"
            state.record_event("agent_stop", reason=state.stop_reason)
            return _build_response(request, state, decision.user_message)

        # Status is "continue" - execute the chosen tool
        if decision.action:
            # Category F: check autonomy boundary before execution
            if decision.action in BLOCKED_ACTIONS:
                state.status = "blocked"
                state.stop_reason = "autonomy_violation"
                state.record_event("agent_stop", reason=state.stop_reason)
                return _build_response(
                    request, state,
                    f"Action '{decision.action}' is not permitted. "
                    "This agent operates in a sandbox only."
                )

            tool_start = perf_counter()
            try:
                # Pass fault_config to gateway for category D injection
                result = execute_tool(
                    tool_name=decision.action,
                    state=state,
                    arguments=decision.arguments,
                    fault_config=fault_config,
                    max_retries=settings.max_tool_retries,
                )
                tool_latency = (perf_counter() - tool_start) * 1000

                # Feed observation back into context for next iteration
                state.observations.append({
                    "step": state.step,
                    "tool": decision.action,
                    "result": result[:500],  # bound observation size
                })
                state.record_event("tool_executed", tool=decision.action,
                                  result_preview=result[:200])

                # If the tool result indicates a transient error, the model
                # can decide to retry or handle it on the next loop iteration
                if result.startswith("TOOL TIMEOUT:") or result.startswith("TOOL EXCEPTION:"):
                    state.record_event("tool_failure_observed", tool=decision.action)

            except ValueError as exc:
                # Tool rejected the call (unknown tool, blocked action, bad args)
                tool_latency = (perf_counter() - tool_start) * 1000
                state.record_error("tool_rejected", str(exc))

                # If blocked by autonomy boundary, stop immediately
                if "blocked by the autonomy boundary" in str(exc):
                    state.status = "blocked"
                    state.stop_reason = "autonomy_violation"
                    state.record_event("agent_stop", reason=state.stop_reason)
                    return _build_response(request, state,
                        f"Action '{decision.action}' is not permitted. "
                        "This agent operates in a sandbox only.")

                # Otherwise feed the error back as an observation
                state.observations.append({
                    "step": state.step,
                    "tool": decision.action,
                    "result": f"ERROR: {exc}",
                })

            except Exception as exc:
                # Unexpected tool failure (category D)
                tool_latency = (perf_counter() - tool_start) * 1000
                state.record_error("tool_exception", str(exc))
                state.observations.append({
                    "step": state.step,
                    "tool": decision.action,
                    "result": f"TOOL EXCEPTION: {exc}",
                })

    # --- 3. Budget exhausted (category E) ---
    if state.status == "continue":
        state.status = "budget_exceeded"
        state.stop_reason = "step_budget_reached"
        state.record_event("agent_stop", reason=state.stop_reason)

    return _build_response(request, state)


# ---------------------------------------------------------------------------
# Response builder
# ---------------------------------------------------------------------------

def _build_response(
    request, state: AgentState, user_message: str | None = None,
) -> ArenaResponse:
    """Convert AgentState into the ArenaResponse contract.

    Always returns a valid ArenaResponse, even if everything went wrong.
    This satisfies the spec requirement that /arena/run never crashes.
    """
    # Build a final_response from user_message, ledger summary, or fallback
    if user_message:
        final = user_message
    elif state.ledger:
        total = sum(e.amount for e in state.ledger)
        cats = {}
        for e in state.ledger:
            cats[e.category] = cats.get(e.category, 0) + e.amount
        cat_str = ", ".join(f"{c}: ${a:.2f}" for c, a in sorted(cats.items()))
        final = (
            f"Processed {len(state.ledger)} expense(s). "
            f"Total: ${total:.2f}. Categories: {cat_str}"
        )
    else:
        final = f"Agent stopped: {state.stop_reason}"

    # Ensure final_response is non-empty (ArenaResponse requires min_length=1)
    if not final or not final.strip():
        final = f"Agent stopped with status: {state.status}"

    # Truncate to fit ArenaResponse max_length
    final = final[:2000]

    # Clamp step count to valid range
    steps = min(state.step, 6)

    # Build tool traces as ToolTrace objects
    tool_calls = []
    for t in state.tool_traces:
        try:
            tool_calls.append(ToolTrace.model_validate(t))
        except Exception:
            pass  # Skip malformed traces

    return ArenaResponse(
        request_id=request.request_id,
        status=state.status,
        final_response=final,
        steps=steps,
        stop_reason=state.stop_reason,
        tool_calls=tool_calls,
        errors=state.errors,
        events=state.events,
        metrics=Metrics(
            model_calls=min(state.model_calls, 6),
            input_tokens=state.input_tokens or None,
            output_tokens=state.output_tokens or None,
            estimated_cost_usd=round(state.estimated_cost_usd, 6) or None,
        ),
    )
