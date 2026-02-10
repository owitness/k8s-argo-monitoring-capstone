FROM python:3.11-slim

WORKDIR /app

# Install system dependencies for Ansible
RUN apt-get update && apt-get install -y --no-install-recommends \
    openssh-client \
    git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ────────────── debug ──────────────
COPY main.py .
RUN ls -la /app && cat /app/main.py && which ansible-playbook
# ───────────────────────────────────

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
