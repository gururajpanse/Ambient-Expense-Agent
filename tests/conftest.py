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

import os

import google.auth
from google.auth.credentials import AnonymousCredentials


# Mock google.auth.default to prevent DefaultCredentialsError when credentials are missing locally
def mock_default(*args, **kwargs):
    return AnonymousCredentials(), "mock-project-id"


google.auth.default = mock_default

# Set dummy environment variables to allow local test collection without GCP credentials
os.environ["GOOGLE_CLOUD_PROJECT"] = "mock-project-id"
os.environ["GOOGLE_CLOUD_LOCATION"] = "global"
os.environ["GEMINI_API_KEY"] = "mock-api-key"
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "False"
