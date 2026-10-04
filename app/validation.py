"""Validation layer: schema check (Pydantic) + semantic checks (business rules).

Two-stage validation:
  1. Schema — does the JSON parse into AgentDecision?
  2. Semantic — does the decision make *sense* given current state?

Recovery policy: up to max_repair_retries attempts, then a typed contract_error.
"""

from __future__ import annotations
import json
import logging
from pydantic import ValidationError

from app.models import AgentDecision, ALLOWED_CATEGORIES, AgentState

log = logging.getLogger("validation")


# ---------------------------------------------------------------------------
# 1. Schema validation — parse raw LLM text into AgentDecision
# ---------------------------------------------------------------------------

def parse_decision(raw_text: str) -> AgentDecision:
    """Parse LLM output into AgentDecision.  Raises ValueError on failure.

    Tries to find JSON in the text (the model may wrap it in markdown fences).
    """
    text = raw_text.strip()

    # Strip markdown code fences if present (```json ... ```)
    if text.startswith("```"):
        # Find the end fence
        lines = text.split("\n")
        # Remove first and last fence lines
        json_lines = []
        inside = False
        for line in lines:
            if line.strip().startswith("```") and not inside:
                inside = True
                continue
            elif line.strip() == "```" and inside:
                break
            elif inside:
                json_lines.append(line)
        text = "\n".join(json_lines).strip()

    # Try direct JSON parse
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Last resort: look for first { ... last }
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError(f"No JSON object found in model output: {text[:200]}")
        try:
            data = json.loads(text[start:end + 1])
        except json.JSONDecodeError as exc:
            raise ValueError(f"Malformed JSON in model output: {exc}") from exc

    # Pydantic schema validation (required fields, types, enums)
    try:
        return AgentDecision.model_validate(data)
    except ValidationError as exc:
        raise ValueError(f"Schema validation failed: {exc}") from exc


# ---------------------------------------------------------------------------
# 2. Semantic validation — business rules on top of valid schema
# ---------------------------------------------------------------------------

# The allow-list of tool names that the agent may call.
TOOL_ALLOW_LIST = frozenset({
    "parse_input",
    "categorize_expense",
    "detect_duplicates",
    "write_ledger",
    "request_clarification",
})

# Actions that are NEVER allowed (autonomy boundary).
BLOCKED_ACTIONS = frozenset({
    "send_email", "delete_file", "make_payment", "execute_code",
    "modify_system", "access_database", "transfer_funds",
})


def validate_semantics(decision: AgentDecision, state: AgentState) -> list[str]:
    """Return a list of semantic violation messages (empty = all good).

    These are *not* schema errors — the JSON was valid.  These are domain
    rules that the model may violate.
    """
    issues: list[str] = []

    # 1. If status is "continue", an action MUST be specified
    if decision.status == "continue" and not decision.action:
        issues.append("Status is 'continue' but no action was specified.")

    # 2. Action must be in the allow-list (if provided)
    if decision.action:
        if decision.action in BLOCKED_ACTIONS:
            issues.append(
                f"Action '{decision.action}' is blocked by the autonomy boundary."
            )
        elif decision.action not in TOOL_ALLOW_LIST:
            issues.append(
                f"Action '{decision.action}' is not a registered tool. "
                f"Allowed: {sorted(TOOL_ALLOW_LIST)}"
            )

    # 3. "completed" requires the ledger to have at least one entry OR
    #    there are no pending rows left to process
    if decision.status == "completed":
        if state.pending_rows and not state.ledger:
            issues.append(
                "Cannot mark 'completed' while there are pending rows "
                "and the ledger is empty."
            )

    # 4. Category validation (if categorize_expense is the action)
    if decision.action == "categorize_expense":
        cat = decision.arguments.get("category", "").lower()
        if cat and cat not in ALLOWED_CATEGORIES:
            issues.append(
                f"Category '{cat}' is not in the allowed list. "
                f"Allowed: {sorted(ALLOWED_CATEGORIES)}"
            )

    # 5. Amount validation (if present in arguments)
    amount = decision.arguments.get("amount")
    if amount is not None:
        try:
            if float(amount) < 0:
                issues.append("Amount cannot be negative.")
        except (ValueError, TypeError):
            issues.append(f"Amount '{amount}' is not a valid number.")

    # 6. needs_clarification must include a user_message
    if decision.status == "needs_clarification" and not decision.user_message:
        issues.append(
            "Status is 'needs_clarification' but no user_message was provided."
        )

    return issues


def build_repair_feedback(raw_text: str, errors: list[str]) -> str:
    """Build a prompt snippet telling the model what went wrong so it can retry."""
    error_list = "\n".join(f"  - {e}" for e in errors)
    return (
        f"Your previous response had validation errors:\n{error_list}\n\n"
        f"Please fix these issues and return a corrected JSON AgentDecision. "
        f"Remember: respond ONLY with a valid JSON object."
    )
