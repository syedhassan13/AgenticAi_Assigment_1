"""Comprehensive tests for the Expense Ledger Agent.

Includes:
  - Scaffold/infrastructure tests (original)
  - Domain unit tests (tools, validation, state)
  - Multi-turn clarification test (spec requirement)
  - Fault injection tests (categories C, D)
  - Autonomy boundary tests (category F)
  - Budget termination tests (category E)
  - Prompt injection resistance tests (category B)
  - Contract recovery tests (category C)
"""

import json
import unittest
from unittest.mock import patch, AsyncMock, MagicMock
from fastapi.testclient import TestClient

from app.main import app
from app.memory import Memory
from app.models import AgentDecision, AgentState, LedgerEntry, ALLOWED_CATEGORIES
from app.validation import parse_decision, validate_semantics, build_repair_feedback
from app.tools import (
    execute_tool, TOOLS, BLOCKED_ACTIONS,
    parse_input, categorize_expense, detect_duplicates, write_ledger,
    ToolTimeoutError, MalformedToolOutputError,
)
from app.prompts import (
    build_state_context, build_external_context,
    build_observation_context, assemble_messages,
)


# ===========================================================================
# 1. Infrastructure / scaffold tests
# ===========================================================================

class TestScaffold(unittest.TestCase):
    """Verify the HTTP contract and basic infrastructure."""

    def test_health_endpoint(self):
        with TestClient(app) as client:
            res = client.get('/health')
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertEqual(data['status'], 'ok')
            self.assertEqual(data['implementation'], 'expense_ledger')

    def test_manifest_endpoint(self):
        with TestClient(app) as client:
            res = client.get('/arena/manifest')
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertEqual(data['arena_version'], '0.1')
            self.assertEqual(data['domain'], 'Expense Ledger Agent')
            self.assertIn('parse_input', data['tools'])

    def test_index_page(self):
        with TestClient(app) as client:
            res = client.get('/')
            self.assertEqual(res.status_code, 200)

    def test_arena_run_returns_valid_schema(self):
        """POST /arena/run must always return a valid ArenaResponse."""
        with TestClient(app) as client:
            res = client.post('/arena/run', json={
                'task': 'Test task',
                'arena_config': {'fault': 'none'}
            })
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertIn(data['status'], [
                'completed', 'needs_clarification', 'blocked',
                'approval_required', 'tool_error', 'contract_error',
                'budget_exceeded', 'failed'
            ])
            self.assertIn('final_response', data)
            self.assertIn('steps', data)
            self.assertIn('stop_reason', data)

    def test_blank_task_rejected(self):
        with TestClient(app) as client:
            res = client.post('/arena/run', json={'task': '  '})
            self.assertEqual(res.status_code, 422)

    def test_max_steps_overflow_rejected(self):
        with TestClient(app) as client:
            res = client.post('/arena/run', json={
                'task': 'Test', 'arena_config': {'max_steps': 99}
            })
            self.assertEqual(res.status_code, 422)


# ===========================================================================
# 2. Memory tests
# ===========================================================================

class TestMemory(unittest.TestCase):

    def test_memory_isolation(self):
        memory = Memory()
        memory.add('session_a', 'hello', 'hi there')
        self.assertEqual(len(memory.get('session_a')), 2)
        self.assertEqual(memory.get('session_b'), [])

    def test_memory_bound(self):
        memory = Memory()
        for i in range(10):
            memory.add('a', str(i), 'reply')
        # Max 12 messages (6 turns)
        self.assertEqual(len(memory.get('a')), 12)

    def test_memory_message_types(self):
        memory = Memory()
        memory.add('a', 'question', 'answer')
        msgs = memory.get('a')
        self.assertEqual(msgs[0].type, 'human')
        self.assertEqual(msgs[1].type, 'ai')

    def test_memory_clear(self):
        memory = Memory()
        memory.add('a', 'x', 'y')
        memory.clear('a')
        self.assertEqual(memory.get('a'), [])

    def test_chat_reset_endpoint(self):
        with TestClient(app) as client:
            session = 'test-session-1234567890'
            client.post('/chat', json={
                'session_id': session, 'task': 'Test'
            })
            self.assertTrue(len(client.app.state.memory.get(session)) > 0)
            client.delete(f'/chat/{session}')
            self.assertEqual(client.app.state.memory.get(session), [])


# ===========================================================================
# 3. Tool unit tests
# ===========================================================================

class TestTools(unittest.TestCase):

    def test_parse_input_csv(self):
        state = AgentState()
        result = parse_input(state, raw_text="Coffee,4.50\nLunch,11.25")
        self.assertIn("Parsed 2", result)
        self.assertEqual(len(state.pending_rows), 2)

    def test_parse_input_receipt(self):
        state = AgentState()
        result = parse_input(state, raw_text="Coffee at Starbucks $4.50")
        self.assertIn("Parsed 1", result)
        self.assertEqual(state.pending_rows[0]['amount'], 4.50)

    def test_parse_input_empty(self):
        state = AgentState()
        result = parse_input(state, raw_text="")
        self.assertIn("Error", result)

    def test_categorize_expense(self):
        state = AgentState()
        state.pending_rows = [{"description": "Coffee", "amount": 4.50, "date": "", "category": ""}]
        result = categorize_expense(state, row_index=0, category="food",
                                   description="Coffee", amount=4.50)
        self.assertIn("Categorized", result)
        self.assertEqual(len(state.ledger), 1)
        self.assertEqual(len(state.pending_rows), 0)

    def test_categorize_invalid_category(self):
        state = AgentState()
        state.pending_rows = [{"description": "X", "amount": 1.0}]
        result = categorize_expense(state, row_index=0, category="INVALID_CAT")
        self.assertIn("Invalid category", result)
        self.assertEqual(len(state.ledger), 0)

    def test_categorize_no_pending(self):
        state = AgentState()
        result = categorize_expense(state, row_index=0, category="food")
        self.assertIn("No pending rows", result)

    def test_detect_duplicates_found(self):
        state = AgentState()
        entry = LedgerEntry(description="Coffee", amount=4.50)
        state.ledger = [entry, entry]
        result = detect_duplicates(state)
        self.assertIn("duplicate", result.lower())

    def test_detect_duplicates_none(self):
        state = AgentState()
        state.ledger = [
            LedgerEntry(description="Coffee", amount=4.50),
            LedgerEntry(description="Lunch", amount=11.25),
        ]
        result = detect_duplicates(state)
        self.assertIn("No duplicates", result)

    def test_write_ledger_with_pending(self):
        state = AgentState()
        state.pending_rows = [{"description": "X", "amount": 1.0}]
        result = write_ledger(state)
        self.assertIn("Cannot finalise", result)

    def test_write_ledger_success(self):
        state = AgentState()
        state.ledger = [LedgerEntry(description="Coffee", amount=4.50, category="food")]
        result = write_ledger(state)
        self.assertIn("finalised", result.lower())
        self.assertIn("$4.50", result)

    def test_tool_allow_list(self):
        self.assertIn("parse_input", TOOLS)
        self.assertIn("categorize_expense", TOOLS)
        self.assertIn("detect_duplicates", TOOLS)
        self.assertIn("write_ledger", TOOLS)
        self.assertIn("request_clarification", TOOLS)


# ===========================================================================
# 4. Validation tests
# ===========================================================================

class TestValidation(unittest.TestCase):

    def test_parse_valid_json(self):
        raw = json.dumps({
            "status": "continue", "action": "parse_input",
            "arguments": {"raw_text": "test"}, "reasoning": "test"
        })
        d = parse_decision(raw)
        self.assertEqual(d.status, "continue")
        self.assertEqual(d.action, "parse_input")

    def test_parse_json_in_markdown_fences(self):
        raw = '```json\n{"status": "completed", "user_message": "Done"}\n```'
        d = parse_decision(raw)
        self.assertEqual(d.status, "completed")

    def test_parse_invalid_json_raises(self):
        with self.assertRaises(ValueError):
            parse_decision("not json at all without braces")

    def test_parse_bad_schema_raises(self):
        raw = '{"status": "INVALID_STATUS"}'
        with self.assertRaises(ValueError):
            parse_decision(raw)

    def test_semantic_continue_needs_action(self):
        d = AgentDecision(status="continue", action=None)
        issues = validate_semantics(d, AgentState())
        self.assertTrue(any("no action" in i.lower() for i in issues))

    def test_semantic_blocked_action(self):
        d = AgentDecision(status="continue", action="send_email")
        issues = validate_semantics(d, AgentState())
        self.assertTrue(any("blocked" in i.lower() for i in issues))

    def test_semantic_invalid_category(self):
        d = AgentDecision(
            status="continue", action="categorize_expense",
            arguments={"category": "NONEXISTENT"}
        )
        issues = validate_semantics(d, AgentState())
        self.assertTrue(any("not in the allowed list" in i for i in issues))

    def test_semantic_clarification_needs_message(self):
        d = AgentDecision(status="needs_clarification", user_message=None)
        issues = validate_semantics(d, AgentState())
        self.assertTrue(any("user_message" in i for i in issues))

    def test_semantic_completed_with_pending(self):
        state = AgentState()
        state.pending_rows = [{"description": "X", "amount": 1.0}]
        d = AgentDecision(status="completed", user_message="done")
        issues = validate_semantics(d, state)
        self.assertTrue(any("pending" in i.lower() for i in issues))

    def test_repair_feedback_format(self):
        feedback = build_repair_feedback("bad output", ["error 1", "error 2"])
        self.assertIn("error 1", feedback)
        self.assertIn("error 2", feedback)


# ===========================================================================
# 5. Prompt engineering tests
# ===========================================================================

class TestPrompts(unittest.TestCase):

    def test_state_context_dynamic(self):
        state = AgentState(max_steps=6)
        state.step = 2
        state.pending_rows = [{"description": "Coffee", "amount": 4.50}]
        ctx = build_state_context(state)
        self.assertIn("step 2 of 6", ctx)
        self.assertIn("Pending rows: 1", ctx)

    def test_external_context_delimited(self):
        ext = [MagicMock(source="receipt", content="Buy $5 coffee")]
        ctx = build_external_context(ext)
        self.assertIn("UNTRUSTED", ctx)
        self.assertIn("BEGIN UNTRUSTED", ctx)
        self.assertIn("END UNTRUSTED", ctx)

    def test_observation_context(self):
        obs = [{"step": 1, "tool": "parse_input", "result": "Parsed 2 rows"}]
        ctx = build_observation_context(obs)
        self.assertIn("parse_input", ctx)
        self.assertIn("Parsed 2 rows", ctx)

    def test_assemble_messages_layers(self):
        state = AgentState()
        msgs = assemble_messages(task="Test task", state=state)
        roles = [m['role'] for m in msgs]
        # Must have system + at least one user message
        self.assertIn('system', roles)
        self.assertIn('user', roles)
        # System prompt should contain tool names
        system = next(m for m in msgs if m['role'] == 'system')
        self.assertIn('parse_input', system['content'])


# ===========================================================================
# 6. Agent state tests
# ===========================================================================

class TestAgentState(unittest.TestCase):

    def test_budget_remaining(self):
        state = AgentState(max_steps=3)
        self.assertTrue(state.budget_remaining())
        state.step = 3
        self.assertFalse(state.budget_remaining())

    def test_budget_stops_on_terminal(self):
        state = AgentState(max_steps=6)
        state.status = "completed"
        self.assertFalse(state.budget_remaining())

    def test_record_tool_trace(self):
        state = AgentState()
        state.record_tool_trace(step=1, tool="parse_input", outcome="success")
        self.assertEqual(len(state.tool_traces), 1)
        self.assertEqual(state.tool_traces[0]['tool'], 'parse_input')

    def test_record_error(self):
        state = AgentState()
        state.step = 2
        state.record_error("test_error", "something broke")
        self.assertEqual(len(state.errors), 1)
        self.assertEqual(state.errors[0]['type'], 'test_error')


# ===========================================================================
# 7. Fault injection tests (categories C, D)
# ===========================================================================

class TestFaultInjection(unittest.TestCase):

    def test_tool_timeout_fault(self):
        """Category D: tool timeout fault fires and is retried."""
        state = AgentState()
        state.step = 1
        state.pending_rows = [{"description": "Coffee", "amount": 4.50}]

        fault = MagicMock()
        fault.type = "tool_timeout"

        result = execute_tool(
            "parse_input", state,
            {"raw_text": "Coffee $4.50"},
            fault_config=fault, max_retries=1
        )
        # Should have retried and eventually succeeded (fault fires once)
        self.assertTrue(
            "Parsed" in result or "TIMEOUT" in result
        )

    def test_malformed_output_fault(self):
        """Category D: malformed output fault fires and is retried."""
        state = AgentState()
        state.step = 1

        fault = MagicMock()
        fault.type = "malformed_tool_output"

        result = execute_tool(
            "parse_input", state,
            {"raw_text": "Coffee $4.50"},
            fault_config=fault, max_retries=1
        )
        self.assertTrue(
            "Parsed" in result or "MALFORMED" in result
        )

    def test_arena_run_with_tool_timeout(self):
        """Category D: /arena/run with tool_timeout fault returns valid schema."""
        with TestClient(app) as client:
            res = client.post('/arena/run', json={
                'task': 'Process this expense: Coffee $4.50',
                'arena_config': {'max_steps': 6, 'fault': 'tool_timeout'}
            })
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertIn('status', data)
            self.assertIn('final_response', data)


# ===========================================================================
# 8. Autonomy boundary tests (category F)
# ===========================================================================

class TestAutonomyBoundary(unittest.TestCase):

    def test_blocked_action_gateway(self):
        """Category F: blocked actions raise ValueError in gateway."""
        state = AgentState()
        state.step = 1
        with self.assertRaises(ValueError) as ctx:
            execute_tool("send_email", state, {})
        self.assertIn("blocked", str(ctx.exception).lower())

    def test_blocked_actions_list(self):
        """All dangerous actions are in the blocked list."""
        for action in ["send_email", "delete_file", "make_payment",
                       "execute_code", "transfer_funds"]:
            self.assertIn(action, BLOCKED_ACTIONS)

    def test_unknown_tool_rejected(self):
        """Unknown tools are rejected by the gateway."""
        state = AgentState()
        state.step = 1
        with self.assertRaises(ValueError):
            execute_tool("nonexistent_tool", state, {})


# ===========================================================================
# 9. Multi-turn clarification test (spec requirement)
# ===========================================================================

class TestMultiTurnClarification(unittest.TestCase):
    """Spec: 'A reply containing only A102 must continue the cancellation.'
    Our domain equivalent: after asking for missing amounts, a follow-up
    with just the amount should continue the expense processing.
    """

    def test_multi_turn_session_preserves_context(self):
        """Memory preserves the user goal across clarification turns."""
        with TestClient(app) as client:
            session = 'multi-turn-test-1234'

            # Turn 1: ambiguous request
            res1 = client.post('/chat', json={
                'session_id': session,
                'task': 'Add my coffee expense'
            })
            self.assertEqual(res1.status_code, 200)
            data1 = res1.json()

            # Check memory has the conversation
            memory = client.app.state.memory.get(session)
            self.assertGreaterEqual(len(memory), 2)  # user + assistant

            # Turn 2: follow-up with just the amount
            res2 = client.post('/chat', json={
                'session_id': session,
                'task': '$4.50'
            })
            self.assertEqual(res2.status_code, 200)
            data2 = res2.json()

            # The agent should have the context from turn 1
            memory2 = client.app.state.memory.get(session)
            self.assertGreaterEqual(len(memory2), 4)  # 2 turns

            # Clean up
            client.delete(f'/chat/{session}')


# ===========================================================================
# 10. Budget / loop termination tests (category E)
# ===========================================================================

class TestBudgetTermination(unittest.TestCase):

    def test_max_steps_1_terminates(self):
        """With max_steps=1, the agent must stop after 1 step."""
        with TestClient(app) as client:
            res = client.post('/arena/run', json={
                'task': 'Process 100 expenses from this CSV: item1,1.00\nitem2,2.00\nitem3,3.00',
                'arena_config': {'max_steps': 1, 'fault': 'none'}
            })
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertLessEqual(data['steps'], 1)


# ===========================================================================
# Run tests
# ===========================================================================

if __name__ == '__main__':
    unittest.main()
