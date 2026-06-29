# Use python 3.11 slim image
FROM python:3.11-slim

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=off \
    PIP_DISABLE_PIP_VERSION_CHECK=on \
    PORT=7860

# Install system dependencies (runs as root by default)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install uv globally (runs as root)
RUN curl -LsSf https://astral.sh/uv/install.sh | sh

# Set the working directory
WORKDIR /app

# Copy dependency files
COPY pyproject.toml uv.lock ./

# Install python dependencies into the system python environment (runs as root)
RUN uv pip install --system --no-cache -r pyproject.toml

# Set up a new user 'user' with UID 1000 (required for Hugging Face Spaces)
RUN useradd -m -u 1000 user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH

# Copy the rest of the application files and ensure they are owned by the non-root user
COPY --chown=user expense_agent/ /app/expense_agent/
COPY --chown=user agents-cli-manifest.yaml /app/
COPY --chown=user GEMINI.md /app/

# Switch to the non-root user for runtime
USER user

# Expose Hugging Face Space port
EXPOSE 7860

# Run the FastAPI server
CMD ["uvicorn", "expense_agent.fast_api_app:app", "--host", "0.0.0.0", "--port", "7860"]
