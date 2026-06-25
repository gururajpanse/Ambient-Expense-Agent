# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Ambient agent that processes expense reports.

This agent receives expense events via triggers and routes them through a graph-based workflow:
- Expenses under $100 are auto-approved instantly.
- Expenses of $100 or more are reviewed by a Gemini model for risk factors,
  which raises an alert and pauses for human manager approval using ADK 2.0's RequestInput (HITL).
"""

import base64
import json
import re
from enum import Enum

from google.adk import Agent, Context, Event, Workflow
from google.adk.apps import App
from google.adk.events import RequestInput
from google.adk.models import Gemini
from google.genai import types
from pydantic import BaseModel, Field

from .config import config

# ---------------------------------------------------------------------------
# Pydantic schemas for structured data flow
# ---------------------------------------------------------------------------


class ExpenseData(BaseModel):
    """Expense report data extracted from the incoming trigger event."""

    amount: float = Field(description="Expense amount in USD")
    submitter: str = Field(description="Email of the person who submitted")
    category: str = Field(description="Expense category, e.g. travel, meals")
    description: str = Field(description="What the expense is for")
    date: str = Field(description="Date of the expense (YYYY-MM-DD)")


# ---------------------------------------------------------------------------
# Function nodes
# ---------------------------------------------------------------------------


def parse_expense_email(node_input) -> Event:
    """Parse a trigger event and extract expense data.

    The trigger endpoint delivers the raw event JSON. The expense payload lives
    in the ``data`` field, which may be base64-encoded (real Pub/Sub) or
    plain JSON (local testing).
    """
    if hasattr(node_input, "parts") and node_input.parts:
        text_input = "".join([part.text for part in node_input.parts if part.text])
    elif isinstance(node_input, str):
        text_input = node_input
    else:
        text_input = str(node_input)

    try:
        event = json.loads(text_input)
    except json.JSONDecodeError:
        return Event(output={"error": f"Invalid JSON: {text_input[:200]}"})

    if not isinstance(event, dict):
        return Event(output={"error": "JSON payload must be a dictionary."})

    data = event.get("data", event)

    if isinstance(data, str):
        try:
            decoded_bytes = base64.b64decode(data)
            data = json.loads(decoded_bytes)
        except Exception:
            try:
                data = json.loads(data)
            except Exception:
                return Event(output={"error": f"Failed to decode data: {data[:200]}"})

    return Event(
        output={
            "amount": float(data.get("amount", 0)),
            "submitter": data.get("submitter", "unknown"),
            "category": data.get("category", "other"),
            "description": data.get("description", ""),
            "date": data.get("date", ""),
        }
    )


def route_by_amount(node_input: dict, ctx: Context) -> Event:
    """Route expenses based on the configured dollar threshold.

    Returns a routing event that the workflow uses to pick the next
    node: ``AUTO_APPROVE`` for amounts under $100, ``NEEDS_REVIEW``
    for $100 and above.
    """
    ctx.state["expense_data"] = node_input
    amount = node_input.get("amount", 0)
    if amount >= config.review_threshold:
        return Event(route="NEEDS_REVIEW", output=node_input)
    return Event(route="AUTO_APPROVE", output=node_input)


def auto_approve(node_input: dict) -> Event:
    """Auto-approve a low-value expense and log the decision."""
    log_entry = {
        "severity": "INFO",
        "message": (
            f"Expense auto-approved: ${node_input['amount']:.2f}"
            f" from {node_input['submitter']}"
        ),
        "decision": "approved",
        "amount": node_input["amount"],
        "submitter": node_input["submitter"],
        "category": node_input["category"],
    }
    msg = (
        f"Expense auto-approved: ${node_input['amount']:.2f}"
        f" from {node_input['submitter']}"
    )
    print(json.dumps(log_entry), flush=True)
    return Event(
        output={"status": "approved", **node_input},
        content=types.Content(role="model", parts=[types.Part.from_text(text=msg)]),
    )


def security_checkpoint(node_input: dict, ctx: Context) -> Event:
    """Security checkpoint to scrub PII and defend against prompt injections.

    1. Scrubs SSNs (###-##-####) and Credit Card numbers (13-16 digits) from description.
    2. Checks for prompt injection keywords. If found, route straight to request_approval.
    """
    # Reset security state for this run to avoid picking up stale state from previous runs in the same session
    ctx.state["security_event"] = False
    ctx.state["redacted_types"] = []

    desc = node_input.get("description", "")
    redacted_types = []

    # Regex patterns
    ssn_pattern = r"\b\d{3}-\d{2}-\d{4}\b"
    cc_pattern = r"\b(?:\d[ -]*?){13,16}\b"

    # Check and redact SSN
    if re.search(ssn_pattern, desc):
        desc = re.sub(ssn_pattern, "[REDACTED_SSN]", desc)
        redacted_types.append("SSN")

    # Check and redact Credit Card
    if re.search(cc_pattern, desc):
        desc = re.sub(cc_pattern, "[REDACTED_CC]", desc)
        redacted_types.append("Credit Card")

    # Save redacted types to state and update the input/state description
    if redacted_types:
        ctx.state["redacted_types"] = redacted_types
        node_input["description"] = desc
        ctx.state["expense_data"] = node_input

    # Jailbreak / Prompt Injection detection
    injection_keywords = [
        "ignore previous instructions",
        "ignore instructions",
        "system prompt",
        "ignore rules",
        "ignore the rules",
        "bypass review",
        "auto-approve this",
        "you are now",
        "ignore the above",
        "override settings",
    ]

    is_injection = any(kw in desc.lower() for kw in injection_keywords)

    if is_injection:
        log_entry = {
            "severity": "WARNING",
            "message": "Security Alert: Possible prompt injection detected in expense description.",
            "alert_type": "security_injection",
            "submitter": node_input.get("submitter"),
            "amount": node_input.get("amount"),
        }
        print(json.dumps(log_entry), flush=True)
        ctx.state["security_event"] = True
        return Event(route="INJECTION_SUSPECTED", output=node_input)

    return Event(route="CLEAN", output=node_input)


# ---------------------------------------------------------------------------
# LLM review agent & alert tool
# ---------------------------------------------------------------------------


def emit_expense_alert(
    submitter: str,
    amount: float,
    category: str,
    risk_summary: str,
) -> dict:
    """Emit a structured log alerting finance to review a high-value expense.

    Args:
        submitter: Who submitted the expense.
        amount: The expense amount in USD.
        category: The expense category.
        risk_summary: Why this expense needs review.

    Returns:
        Confirmation that the alert was emitted.
    """
    log_entry = {
        "severity": "WARNING",
        "message": (
            f"Expense review alert: ${amount:.2f} from {submitter} — {risk_summary}"
        ),
        "alert_type": "expense_review",
        "submitter": submitter,
        "amount": amount,
        "category": category,
        "risk_summary": risk_summary,
    }
    print(json.dumps(log_entry), flush=True)
    return {"status": "alert_emitted", "submitter": submitter, "amount": amount}


review_agent = Agent(
    name="review_agent",
    model=Gemini(
        model=config.model,
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    mode="single_turn",
    instruction="""You are an expense review agent. You receive expense reports
of $100 or more that need review before approval.

Analyze the expense and:
1. Check for risk factors: unusual category for the amount, vague description,
   suspiciously round numbers, very high value (>$1000), or potential policy
   violations.
2. Call the `emit_expense_alert` tool with the submitter, amount, category,
   and a brief risk summary explaining why this expense needs human review.
3. Return a structured review.

Your review MUST include:
- **Amount**: The expense amount
- **Submitter**: Who submitted it
- **Category**: The expense category
- **Risk level**: low, medium, or high
- **Risk factors**: What flags you found (if any)
- **Recommendation**: approve, request-more-info, or escalate""",
    input_schema=ExpenseData,
    tools=[emit_expense_alert],
)


# ---------------------------------------------------------------------------
# HITL: human approval and final logging
# ---------------------------------------------------------------------------


class ApprovalResponse(BaseModel):
    approve: bool = Field(
        default=True,
        description="Check to Approve, uncheck to Reject."
    )


def request_approval(node_input, ctx: Context):  # type: ignore[no-untyped-def]
    """Pause the workflow and wait for a human to approve or reject.

    Yields a ``RequestInput`` that the ADK runtime surfaces to the UI/API.
    The response becomes the output of this node on resume.
    """
    expense = ctx.state.get("expense_data", {})
    message = "Expense requires manager approval. Approve or reject."
    if ctx.state.get("security_event"):
        message = "⚠️ SECURITY WARNING: Suspicious activity (possible prompt injection) detected. Review carefully. Approve or reject."
    elif ctx.state.get("redacted_types"):
        redacted_str = ", ".join(ctx.state["redacted_types"])
        message = f"Expense requires manager approval (Scrubbed PII: {redacted_str}). Approve or reject."
    yield RequestInput(
        message=message,
        payload=expense,
        response_schema=ApprovalResponse,
    )


def process_decision(node_input, ctx: Context) -> Event:  # type: ignore[no-untyped-def]
    """Process the human's approval decision and log the outcome."""
    approved = True
    if isinstance(node_input, dict):
        approved = node_input.get("approve", True)
    elif hasattr(node_input, "approve"):
        approved = bool(node_input.approve)
    elif isinstance(node_input, str):
        approved = "approve" in node_input.lower()

    status = "approved" if approved else "rejected"
    expense = ctx.state.get("expense_data", {})

    log_entry = {
        "severity": "INFO" if approved else "WARNING",
        "message": f"Expense {status} by manager",
        "decision": status,
    }
    print(json.dumps(log_entry), flush=True)

    submitter = expense.get("submitter", "unknown")
    amount = expense.get("amount", 0)
    category = expense.get("category", "")
    description = expense.get("description", "")
    date = expense.get("date", "")

    parts = [f"${amount:.2f} expense from {submitter} has been {status}."]
    if description:
        parts.append(f'"{description}" ({category}) on {date}.')
    if approved:
        parts.append(
            "The expense has been logged and will be processed for reimbursement."
        )
    else:
        parts.append(
            "The submitter will be notified and may resubmit with additional documentation."
        )

    msg = " ".join(parts)
    return Event(
        output={"status": status, "message": msg},
        content=types.Content(role="model", parts=[types.Part.from_text(text=msg)]),
    )


# ---------------------------------------------------------------------------
# Graph-based workflow
# ---------------------------------------------------------------------------

root_agent = Workflow(
    name="expense_processor",
    edges=[
        ("START", parse_expense_email, route_by_amount),
        (
            route_by_amount,
            {
                "AUTO_APPROVE": auto_approve,
                "NEEDS_REVIEW": security_checkpoint,
            },
        ),
        (
            security_checkpoint,
            {
                "CLEAN": review_agent,
                "INJECTION_SUSPECTED": request_approval,
            },
        ),
        (review_agent, request_approval),
        (request_approval, process_decision),
    ],
)

app = App(
    root_agent=root_agent,
    name="expense_agent",
)
