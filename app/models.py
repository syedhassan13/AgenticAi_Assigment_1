"""Domain-independent HTTP contracts. Add your own decision/state models below."""
from typing import Literal
from uuid import uuid4
from pydantic import BaseModel, ConfigDict, Field, field_validator
class Contract(BaseModel):
    model_config = ConfigDict(extra='forbid')
class ExternalContext(Contract):
    source: str = Field(min_length=1, max_length=100)
    content: str = Field(max_length=10000)
    trust: Literal['untrusted'] = 'untrusted'
class Fault(Contract):
    type: Literal['none', 'tool_timeout', 'malformed_tool_output', 'invalid_agent_decision'] = 'none'
    trigger: Literal['first_matching_operation'] = 'first_matching_operation'
class ArenaConfig(Contract):
    max_steps: int = Field(default=6, ge=1, le=6)
    fault: Fault = Field(default_factory=Fault)
    @field_validator('fault', mode='before')
    @classmethod
    def accept_assignment_shorthand(cls, value):
        return {'type': value} if isinstance(value, str) else value
class ArenaRequest(Contract):
    arena_version: Literal['0.1'] = '0.1'
    request_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=80)
    task: str = Field(min_length=1, max_length=10000)
    external_context: list[ExternalContext] = Field(default_factory=list, max_length=20)
    arena_config: ArenaConfig = Field(default_factory=ArenaConfig)
    @field_validator('task')
    @classmethod
    def nonblank(cls, value):
        if not value.strip(): raise ValueError('Task must not be blank')
        return value
class ToolTrace(Contract):
    step: int = Field(ge=1, le=6)
    tool: str
    attempt: int = Field(default=1, ge=1, le=3)
    outcome: Literal['success', 'timeout', 'malformed_output', 'rejected', 'exception']
    latency_ms: float = Field(default=0, ge=0)
class Metrics(Contract):
    latency_ms: float = Field(default=0, ge=0)
    model_calls: int = Field(default=0, ge=0, le=6)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    estimated_cost_usd: float | None = Field(default=None, ge=0)
class ArenaResponse(Contract):
    arena_version: Literal['0.1'] = '0.1'
    request_id: str
    status: Literal['completed', 'needs_clarification', 'blocked', 'approval_required', 'tool_error', 'contract_error', 'budget_exceeded', 'failed']
    final_response: str = Field(min_length=1, max_length=2000)
    steps: int = Field(default=0, ge=0, le=6)
    stop_reason: str
    tool_calls: list[ToolTrace] = Field(default_factory=list)
    errors: list[dict] = Field(default_factory=list)
    events: list[dict] = Field(default_factory=list)
    metrics: Metrics = Field(default_factory=Metrics)
class ChatRequest(ArenaRequest):
    session_id: str = Field(min_length=16, max_length=80)
    model: str = Field(default='unconfigured', max_length=120)

# ---------------------------------------------------------------------------
# Domain models: Expense Ledger Agent
# ---------------------------------------------------------------------------

class AgentDecision(Contract):
    """What the LLM must return each step — parsed from JSON, then validated.

    The model picks status + action; the *system* enforces limits and allow-lists.
    """
    status: Literal[
        "continue",            # more work to do
        "needs_clarification", # ambiguous / missing info → ask user
        "completed",           # goal reached
        "blocked",             # cannot proceed (autonomy / dependency)
        "failed",              # unrecoverable domain error
    ]
    action: str | None = None                  # tool name from the allow-list
    arguments: dict = Field(default_factory=dict)  # tool kwargs
    user_message: str | None = None            # message shown to the user
    reasoning: str = ""                        # brief chain-of-thought (for logs)


class LedgerEntry(Contract):
    """One categorised expense row in the sandbox ledger."""
    description: str = Field(min_length=1, max_length=500)
    amount: float = Field(ge=0)                # non-negative
    currency: str = Field(default="USD", max_length=10)
    date: str = ""                             # ISO-ish or empty
    category: str = Field(default="uncategorized", max_length=60)
    source_text: str = ""                      # original input fragment


# Allowed expense categories — used for semantic validation of LLM choices.
ALLOWED_CATEGORIES = frozenset({
    "food", "transport", "utilities", "entertainment", "healthcare",
    "housing", "education", "shopping", "travel", "subscriptions",
    "office", "personal", "gifts", "insurance", "taxes",
    "uncategorized", "other",
})


class AgentState:
    """Per-run mutable state — NOT a Pydantic model so we can mutate freely.

    Tracks everything the loop needs: step count, sandbox ledger, observations,
    tool traces, errors, and token/cost accounting.
    """

    def __init__(self, max_steps: int = 6, max_retries: int = 2):
        # --- execution limits (system-owned) ---
        self.max_steps = max_steps
        self.max_retries = max_retries

        # --- progress ---
        self.step = 0               # current step (1-indexed once loop starts)
        self.status = "continue"    # mirrors ArenaResponse.status
        self.stop_reason = ""

        # --- domain sandbox ---
        self.pending_rows: list[dict] = []          # rows parsed but not yet categorised
        self.ledger: list[LedgerEntry] = []         # final categorised entries
        self.duplicates_found: list[dict] = []      # detected duplicate info

        # --- observations fed back into context ---
        self.observations: list[dict] = []          # {step, tool, result}

        # --- Arena bookkeeping ---
        self.tool_traces: list[dict] = []           # for ArenaResponse.tool_calls
        self.errors: list[dict] = []                # for ArenaResponse.errors
        self.events: list[dict] = []                # for ArenaResponse.events

        # --- token / cost accounting ---
        self.model_calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.estimated_cost_usd = 0.0

    # Helpers ------------------------------------------------------------------

    def budget_remaining(self) -> bool:
        """True if the agent can take another step."""
        return self.step < self.max_steps and self.status == "continue"

    def record_tool_trace(self, step: int, tool: str, outcome: str,
                          latency_ms: float = 0, attempt: int = 1):
        self.tool_traces.append({
            "step": step, "tool": tool, "attempt": attempt,
            "outcome": outcome, "latency_ms": round(latency_ms, 1),
        })

    def record_error(self, error_type: str, message: str, step: int | None = None):
        self.errors.append({
            "type": error_type, "message": message,
            "step": step or self.step,
        })

    def record_event(self, event: str, **extra):
        self.events.append({"step": self.step, "event": event, **extra})
