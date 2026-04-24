# Containerises the existing Python trading engine as-is.
# Build context is the repo root (../), not autoflow/.
FROM python:3.12-slim

WORKDIR /engine
COPY requirements.txt /engine/requirements.txt
RUN pip install --no-cache-dir -r /engine/requirements.txt

# Source is bind-mounted in dev; COPY here is a fallback for prod builds.
COPY . /engine

ENV PYTHONUNBUFFERED=1
CMD ["python", "run_strategy.py", "--config", "config/config.paper.yaml"]
