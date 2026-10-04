"""Sandbox tools for the Expense Ledger Agent.

Five tools (spec rule 3):
  parse_input          - extract expense rows from receipt text or CSV
  categorize_expense   - assign a category to a parsed row
  detect_duplicates    - check ledger for duplicate entries
  write_ledger         - finalise the ledger
  request_clarification - ask the user for missing info

All tools operate on the in-memory AgentState sandbox.
No real payments, deletes, or emails - enforced by the allow-list gateway.

Fault injection (spec section 11, category D):
  The execute_tool gateway intercepts arena_config.fault and simulates
  tool_timeout, malformed_tool_output on the first matching operation.
"""

from __future__ import annotations
import asyncio
import logging
import re
import time
from app.models import AgentState, LedgerEntry, ALLOWED_CATEGORIES

log = logging.getLogger("tools")


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def parse_input(state: AgentState, raw_text: str = "", **kwargs) -> str:
    """Extract expense rows from raw receipt text or CSV lines.

    Simple heuristic parser - looks for amount patterns and descriptions.
    The model decides WHEN to call this; this tool decides HOW to parse.
    """
    if not raw_text.strip():
        return "Error: no raw_text provided to parse."

    rows = []
    lines = raw_text.strip().split("\n")

    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        # Try CSV-style: description,amount[,date][,category]
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 2:
            desc = parts[0]
            # Find the first part that looks like a number
            amount = None
            date = ""
            for p in parts[1:]:
                cleaned = re.sub(r"[pounds|euro|dollar|yen|rupee|$|EUR|USD|GBP]", "", p).strip()
                cleaned = re.sub(r"[^0-9.\-]", "", cleaned).strip()
                try:
                    val = float(cleaned)
                    if val >= 0:
                        amount = round(val, 2)
                        break
                except ValueError:
                    pass
                # Might be a date
                if re.match(r"\d{4}[-/]\d{2}[-/]\d{2}", p.strip()):
                    date = p.strip()

            if amount is not None and desc:
                rows.append({
                    "description": desc[:200],
                    "amount": amount,
                    "date": date,
                    "category": "",
                    "source_text": line[:300],
                })
                continue

        # Try free-text: look for currency amounts like $12.50, 45.00, etc.
        amounts = re.findall(r"[\$\u00a3\u20ac\u00a5\u20b9]?\s*(\d+(?:\.\d{1,2})?)", line)
        if amounts:
            # Use the line as description, first amount found
            amount_val = round(float(amounts[0]), 2)
            # Try to extract a date
            date_match = re.search(r"\d{4}[-/]\d{2}[-/]\d{2}", line)
            # Clean description by removing the amount
            desc_cleaned = re.sub(r"[\$\u00a3\u20ac\u00a5\u20b9]?\s*\d+(?:\.\d{1,2})?", "", line).strip()
            rows.append({
                "description": desc_cleaned[:200] or line[:200],
                "amount": amount_val,
                "date": date_match.group() if date_match else "",
                "category": "",
                "source_text": line[:300],
            })

    if not rows:
        return "No expense rows could be extracted from the input. The text may be ambiguous or missing amounts."

    # Store parsed rows in state as pending
    state.pending_rows.extend(rows)
    summary = "; ".join(
        f"{r['description'][:30]}=${r['amount']}" for r in rows
    )
    return f"Parsed {len(rows)} expense row(s): {summary}"


def categorize_expense(
    state: AgentState,
    row_index: int = 0,
    category: str = "uncategorized",
    description: str = "",
    amount: float | None = None,
    **kwargs,
) -> str:
    """Assign a category to a pending expense row and move it to the ledger.

    The model chooses the category; this tool validates it against the allow-list.
    """
    category = category.lower().strip()

    # Validate category against allow-list
    if category not in ALLOWED_CATEGORIES:
        return (
            f"Invalid category '{category}'. "
            f"Allowed: {', '.join(sorted(ALLOWED_CATEGORIES))}"
        )

    # Get the pending row
    if not state.pending_rows:
        return "No pending rows to categorize."

    if row_index < 0 or row_index >= len(state.pending_rows):
        return f"Invalid row_index {row_index}. Valid range: 0-{len(state.pending_rows) - 1}"

    row = state.pending_rows[row_index]

    # Build the ledger entry
    entry = LedgerEntry(
        description=description or row.get("description", "Unknown"),
        amount=amount if amount is not None else row.get("amount", 0.0),
        date=row.get("date", ""),
        category=category,
        source_text=row.get("source_text", ""),
    )

    state.ledger.append(entry)
    state.pending_rows.pop(row_index)

    return (
        f"Categorized: '{entry.description}' -> {entry.category} "
        f"(${entry.amount:.2f}). "
        f"Remaining pending: {len(state.pending_rows)}"
    )


def detect_duplicates(state: AgentState, **kwargs) -> str:
    """Check the current ledger for likely duplicate entries.

    Compares description + amount pairs. No side effects.
    """
    if len(state.ledger) < 2:
        return "Not enough ledger entries to check for duplicates."

    seen: dict[str, list[int]] = {}
    for i, entry in enumerate(state.ledger):
        # Normalise key: lowercase description + amount
        key = f"{entry.description.lower().strip()}|{entry.amount:.2f}"
        seen.setdefault(key, []).append(i)

    dupes = {k: v for k, v in seen.items() if len(v) > 1}

    if not dupes:
        state.duplicates_found = []
        return "No duplicates detected in the ledger."

    state.duplicates_found = [
        {"key": k, "indices": v} for k, v in dupes.items()
    ]
    dupe_desc = "; ".join(
        f"rows {v} share '{k.split('|')[0]}'" for k, v in dupes.items()
    )
    return f"Found {len(dupes)} potential duplicate group(s): {dupe_desc}"


def write_ledger(state: AgentState, **kwargs) -> str:
    """Finalise the ledger.  Requires all pending rows to be processed.

    This is a sandbox operation - no real file I/O.
    """
    if state.pending_rows:
        return (
            f"Cannot finalise: {len(state.pending_rows)} row(s) still pending. "
            "Categorize all rows first."
        )

    if not state.ledger:
        return "Cannot finalise: the ledger is empty. Parse and categorize expenses first."

    total = sum(e.amount for e in state.ledger)
    categories = {}
    for e in state.ledger:
        categories[e.category] = categories.get(e.category, 0) + e.amount

    cat_summary = ", ".join(
        f"{cat}: ${amt:.2f}" for cat, amt in sorted(categories.items())
    )

    return (
        f"Ledger finalised with {len(state.ledger)} entries. "
        f"Total: ${total:.2f}. By category: {cat_summary}"
    )


def request_clarification(state: AgentState, question: str = "", **kwargs) -> str:
    """Record that clarification is needed.  The agent loop uses this to stop."""
    if not question:
        return "No question provided for clarification."
    return f"Clarification requested: {question}"


# ---------------------------------------------------------------------------
# Tool registry - the allow-list (spec rule 3)
# ---------------------------------------------------------------------------

TOOLS: dict[str, callable] = {
    "parse_input": parse_input,
    "categorize_expense": categorize_expense,
    "detect_duplicates": detect_duplicates,
    "write_ledger": write_ledger,
    "request_clarification": request_clarification,
}

# Blocked actions - autonomy boundary (spec section 10, category F)
BLOCKED_ACTIONS = frozenset({
    "send_email", "delete_file", "make_payment", "execute_code",
    "modify_system", "access_database", "transfer_funds",
    "send_notification", "delete_record", "update_account",
    "process_payment", "wire_transfer", "rm", "drop_table",
})


# ---------------------------------------------------------------------------
# Fault injection (spec section 11, categories C and D)
# ---------------------------------------------------------------------------

class ToolTimeoutError(Exception):
    """Simulated tool timeout for Arena fault injection."""
    pass


class MalformedToolOutputError(Exception):
    """Simulated malformed tool output for Arena fault injection."""
    pass


def _apply_fault(fault_config, tool_name: str, state: AgentState) -> None:
    """Check if this tool call should receive a fault injection.

    Faults fire on the first matching operation only, then mark themselves used.
    This is NOT phrase-matching - it is a general mechanism triggered by config.
    """
    if fault_config is None:
        return
    fault_type = getattr(fault_config, "type", "none")
    if fault_type == "none":
        return

    # Only fire once (first matching operation)
    if getattr(state, "_fault_fired", False):
        return

    if fault_type == "tool_timeout":
        state._fault_fired = True
        log.warning("FAULT INJECTION: tool_timeout on %s", tool_name)
        raise ToolTimeoutError(
            f"Tool '{tool_name}' timed out (simulated Arena fault)."
        )

    if fault_type == "malformed_tool_output":
        state._fault_fired = True
        log.warning("FAULT INJECTION: malformed_tool_output on %s", tool_name)
        raise MalformedToolOutputError(
            f"Tool '{tool_name}' returned malformed output (simulated Arena fault)."
        )


# ---------------------------------------------------------------------------
# Tool execution gateway - single choke-point for allow-list + faults + retry
# ---------------------------------------------------------------------------

def execute_tool(
    tool_name: str,
    state: AgentState,
    arguments: dict,
    fault_config=None,
    max_retries: int = 2,
) -> str:
    """Gateway: look up tool in allow-list, inject faults, execute, return result.

    Retry policy (spec section 9):
      - Up to max_retries attempts on transient failures (timeout, malformed).
      - On permanent failures (unknown tool, blocked action), fail immediately.
      - Returns the result string or raises ValueError for permanent failures.
    """
    # --- Autonomy check: blocked actions (category F) ---
    if tool_name in BLOCKED_ACTIONS:
        raise ValueError(
            f"Action '{tool_name}' is blocked by the autonomy boundary. "
            "This agent only operates in a sandbox."
        )

    # --- Allow-list check ---
    if tool_name not in TOOLS:
        raise ValueError(
            f"Unknown tool '{tool_name}'. Registered tools: {sorted(TOOLS.keys())}"
        )

    tool_fn = TOOLS[tool_name]

    # --- Execute with bounded retry for transient faults ---
    last_error = None
    for attempt in range(1, max_retries + 2):  # 1-indexed, total = max_retries + 1
        try:
            # Apply fault injection BEFORE execution (first matching op only)
            _apply_fault(fault_config, tool_name, state)

            # Execute the tool
            result = tool_fn(state=state, **arguments)

            # Record success trace
            state.record_tool_trace(
                step=state.step, tool=tool_name,
                outcome="success", attempt=attempt,
            )
            return result

        except ToolTimeoutError as exc:
            last_error = exc
            state.record_tool_trace(
                step=state.step, tool=tool_name,
                outcome="timeout", attempt=attempt,
            )
            log.warning("Tool timeout (attempt %d/%d): %s",
                       attempt, max_retries + 1, exc)
            if attempt <= max_retries:
                continue  # Retry
            # Exhausted retries
            state.record_error("tool_timeout", str(exc))
            return f"TOOL TIMEOUT: {tool_name} timed out after {attempt} attempt(s)."

        except MalformedToolOutputError as exc:
            last_error = exc
            state.record_tool_trace(
                step=state.step, tool=tool_name,
                outcome="malformed_output", attempt=attempt,
            )
            log.warning("Malformed tool output (attempt %d/%d): %s",
                       attempt, max_retries + 1, exc)
            if attempt <= max_retries:
                continue  # Retry
            state.record_error("malformed_tool_output", str(exc))
            return f"MALFORMED OUTPUT: {tool_name} returned invalid data after {attempt} attempt(s)."

        except TypeError as exc:
            # Bad arguments - permanent failure, no retry
            state.record_tool_trace(
                step=state.step, tool=tool_name,
                outcome="rejected", attempt=attempt,
            )
            state.record_error("tool_argument_error", str(exc))
            return f"TOOL ERROR: {tool_name} rejected arguments: {exc}"

        except Exception as exc:
            last_error = exc
            state.record_tool_trace(
                step=state.step, tool=tool_name,
                outcome="exception", attempt=attempt,
            )
            log.error("Unexpected tool error (attempt %d/%d): %s",
                     attempt, max_retries + 1, exc)
            if attempt <= max_retries:
                continue  # Retry
            state.record_error("tool_exception", str(exc))
            return f"TOOL EXCEPTION: {tool_name} failed after {attempt} attempt(s): {exc}"

    # Should never reach here, but safety net
    return f"TOOL FAILED: {tool_name} - {last_error}"
