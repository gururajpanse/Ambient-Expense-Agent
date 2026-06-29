# Use python 3.11 slim image
FROM python:3.11-slim

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=off \
    PIP_DISABLE_PIP_VERSION_CHECK=on \
    PORT=7860

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Set up a new user 'user' with UID 1000 (required for Hugging Face Spaces)
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH

# Set the working directory
WORKDIR $HOME/app

# Copy dependency files
COPY --chown=user pyproject.toml uv.lock ./

# Install uv and use it to install python dependencies into the system python environment
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
RUN uv pip install --system --no-cache -r pyproject.toml

# Copy the rest of the application files
COPY --chown=user expense_agent/ ./expense_agent/
COPY --chown=user agents-cli-manifest.yaml ./
COPY --chown=user GEMINI.md ./

# Expose Hugging Face Space port
EXPOSE 7860

# Run the FastAPI server
CMD ["uvicorn", "expense_agent.fast_api_app:app", "--host", "0.0.0.0", "--port", "7860"]
