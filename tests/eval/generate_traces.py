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

"""Trace generator for local evaluations of the expense agent.

Runs the synthetic basic-dataset cases locally through the ADK workflow runner,
intercepts the HITL RequestInput, makes decisions, and serializes results.
"""

import os
import json
import asyncio
import logging
from pathlib import Path
from dotenv import load_dotenv

# Configure logging before loading packages
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("generate_traces")

# Load environment
load_dotenv()

# Set up Vertex AI
import vertexai
vertexai.init(
    project=os.environ.get("GOOGLE_CLOUD_PROJECT", "mock-project-id"),
    location=os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1"),
)

from vertexai import types as val_types
from google.genai import types as genai_types
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from expense_agent.agent import root_agent

async def run_case(runner: Runner, session_service: InMemorySessionService, case: dict) -> dict:
    case_id = case["eval_case_id"]
    prompt_text = case["prompt"]["parts"][0]["text"]
    logger.info(f"Starting case: {case_id}")

    # Create a unique session for this case
    session = await session_service.create_session(
        user_id="eval_user",
        app_name="expense_agent",
        session_id=f"session_{case_id}"
    )

    try:
        # Wrap prompt dict in "data" if not present to match ambient trigger parser behavior
        try:
            expense_dict = json.loads(prompt_text)
            if "data" not in expense_dict:
                payload = {"data": expense_dict}
            else:
                payload = expense_dict
            raw_text = json.dumps(payload)
        except json.JSONDecodeError:
            raw_text = prompt_text

        message = genai_types.Content(
            role="user",
            parts=[genai_types.Part.from_text(text=raw_text)]
        )

        events = []
        # Start runner
        agen = runner.run_async(
            user_id="eval_user",
            session_id=session.id,
            new_message=message,
            yield_user_message=True
        )

        while True:
            try:
                event = await agen.__anext__()
                events.append(event)

                # Check if this event requests manager/human input
                interrupt_id = None
                if event.content and event.content.parts:
                    for part in event.content.parts:
                        if part.function_call and part.function_call.name == "adk_request_input":
                            interrupt_id = part.function_call.id
                            break

                if interrupt_id:
                    # Retrieve the updated session state to see if prompt injection was flagged
                    sess = await session_service.get_session(
                        app_name="expense_agent", user_id="eval_user", session_id=session.id
                    )
                    state = sess.state if sess else {}
                    is_injection = state.get("security_event", False)
                    
                    # Automate HITL approval logic
                    approve = not is_injection
                    logger.info(
                        f"[{case_id}] HITL Intercepted. PII types: {state.get('redacted_types')}. "
                        f"Injection: {is_injection}. Decision: {'APPROVE' if approve else 'REJECT'}"
                    )

                    # Resume workflow with the decision
                    resume_message = genai_types.Content(
                        role="user",
                        parts=[
                            genai_types.Part(
                                function_response=genai_types.FunctionResponse(
                                    id=interrupt_id,
                                    name="adk_request_input",
                                    response={"approve": approve}
                                )
                            )
                        ]
                    )

                    agen = runner.run_async(
                        user_id="eval_user",
                        session_id=session.id,
                        new_message=resume_message,
                        yield_user_message=True
                    )
            except StopAsyncIteration:
                break

        # Format events to standard Vertex Eval format
        trace_events = []
        final_text = ""
        for ev in events:
            if ev.content and ev.content.parts:
                author = ev.author
                if author == "model":
                    author = "expense_processor"
                
                content_dict = ev.content.model_dump(exclude_unset=True, mode="json")
                for part in content_dict.get("parts") or []:
                    part.pop("thought_signature", None)
                    if author == "expense_processor" and part.get("text"):
                        final_text += part["text"] + " "
                
                trace_events.append({
                    "author": author,
                    "content": content_dict
                })

        responses = []
        if final_text:
            responses.append({
                "response": {
                    "role": "model",
                    "parts": [{"text": final_text.strip()}]
                }
            })

        logger.info(f"Finished case: {case_id}")
        return {
            "eval_case_id": case_id,
            "prompt": case["prompt"],
            "agent_data": {
                "turns": [
                    {
                        "turn_index": 0,
                        "turn_id": "turn_0",
                        "events": trace_events
                    }
                ]
            },
            "responses": responses
        }
    except Exception as exc:
        logger.error(f"Error running case {case_id}: {exc}", exc_info=True)
        raise exc

async def main():
    project_root = Path(__file__).resolve().parents[2]
    dataset_path = project_root / "tests" / "eval" / "datasets" / "basic-dataset.json"
    output_path = project_root / "artifacts" / "traces" / "generated_traces.json"

    logger.info(f"Loading dataset from: {dataset_path}")
    with open(dataset_path, "r", encoding="utf-8") as f:
        dataset_data = json.load(f)
    cases = dataset_data.get("eval_cases", [])

    session_service = InMemorySessionService()
    runner = Runner(agent=root_agent, session_service=session_service, app_name="expense_agent")

    merged_cases = []
    for case in cases:
        case_res = await run_case(runner, session_service, case)
        merged_cases.append(val_types.EvalCase.model_validate(case_res))

    result = val_types.EvaluationDataset(eval_cases=merged_cases)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(result.model_dump_json(indent=2, exclude_none=True), encoding="utf-8")
    logger.info(f"Traces written to: {output_path}")

if __name__ == "__main__":
    asyncio.run(main())
