# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import os
import json
from fastapi import FastAPI, Request
from starlette.middleware.base import BaseHTTPMiddleware
import uvicorn
import vertexai
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configure standard Python logging for console logs
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("expense_agent.fast_api_app")

# Initialize Vertex AI at module scope to prevent initializer project errors
vertexai.init(
    project=os.environ.get("GOOGLE_CLOUD_PROJECT", "mock-project-id"),
    location=os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1"),
)

# Custom Middleware to normalize fully-qualified Pub/Sub subscription paths to short names
class PubSubNormalizationMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if "/trigger/pubsub" in request.url.path and request.method == "POST":
            try:
                body = await request.body()
                if body:
                    data = json.loads(body)
                    subscription = data.get("subscription")
                    if subscription:
                        # Extract the subscription name (short name)
                        # e.g., projects/my-project/subscriptions/my-subscription -> my-subscription
                        if "subscriptions/" in subscription:
                            short_name = subscription.split("subscriptions/")[-1]
                        else:
                            short_name = subscription.split("/")[-1]
                        
                        logger.info(f"Normalizing Pub/Sub subscription path from '{subscription}' to '{short_name}'")
                        data["subscription"] = short_name
                        new_body = json.dumps(data).encode("utf-8")
                        
                        # Update the cached body and receive channel so FastAPI uses the normalized payload
                        request._body = new_body
                        async def receive():
                            return {"type": "http.request", "body": new_body, "more_body": False}
                        
                        request._receive = receive
            except Exception as e:
                logger.error(f"Error during Pub/Sub subscription normalization: {e}")
        return await call_next(request)

# Create the FastAPI app using the google-adk get_fast_api_app helper
from google.adk.cli.fast_api import get_fast_api_app

app = get_fast_api_app(
    agents_dir=".",
    web=True,
    otel_to_cloud=False,
    trigger_sources=["pubsub"],
)

# Add the normalization middleware to the app
app.add_middleware(PubSubNormalizationMiddleware)

if __name__ == "__main__":
    uvicorn.run("expense_agent.fast_api_app:app", host="127.0.0.1", port=8080, log_level="info")
