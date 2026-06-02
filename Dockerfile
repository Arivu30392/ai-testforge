FROM python:3.11-slim

# Install Node.js 20 (needed for ADO MCP)
RUN apt-get update && apt-get install -y curl && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project files
COPY main.py api.py index.html ./
COPY knowledge_base.json ./
COPY config/ ./config/

# Create directories
RUN mkdir -p input output logs videos

# Expose port
EXPOSE 8000

# Run API server
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]
