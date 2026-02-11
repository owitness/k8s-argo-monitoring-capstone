FROM python:3.11-slim

WORKDIR /app

# Install system dependencies for Ansible
RUN apt-get update && apt-get install -y --no-install-recommends \
    openssh-client \
    git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Ansible collections during build
RUN ansible-galaxy collection install ibm.ibm_zos_core==1.9.0 --upgrade

COPY main.py .
COPY ansible ./ansible
COPY mainframe_key.pem .
RUN chmod 600 mainframe_key.pem
RUN chmod 600 /app/ansible/mainframe_key.pem

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
