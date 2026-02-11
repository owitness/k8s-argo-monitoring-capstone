from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
import uuid
import boto3
import subprocess
import tempfile
import os
import stat
import shutil
import logging
import yaml
import paramiko  # Added for SSH/SFTP operations
from jinja2 import Environment, BaseLoader
from pathlib import Path
from typing import Optional, Dict

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("automation-api")

app = FastAPI(title="Automation Control Plane - Ansible Runner")

# S3 Configuration
S3_BUCKET = "ansible-playbook-s3-dae"
s3_client = boto3.client('s3')

# Health Endpoint
@app.get("/health")
async def health():
    return {"status": "ok"}


def download_from_s3(bucket: str, s3_path: str, local_path: str) -> bool:
    """Download a file from S3 to local filesystem"""
    try:
        logger.info(f"Downloading s3://{bucket}/{s3_path} to {local_path}")
        s3_client.download_file(bucket, s3_path, local_path)
        logger.info(f"Successfully downloaded {s3_path}")
        return True
    except Exception as e:
        logger.error(f"Error downloading from S3: {str(e)}")
        raise

# Run ansible playbook
async def execute_ansible_playbook(playbook_path, inventory_path, cwd=None):
    execution_id = str(uuid.uuid4())[:8]
    try:
        ansible_cfg_path = os.path.join(cwd or os.getcwd(), 'ansible.cfg')
        with open(ansible_cfg_path, 'w') as f:
            f.write("""[defaults]
forks = 25
host_key_checking = False
""")
        command = [
            "ansible-playbook",
            "-i", inventory_path,
            playbook_path,
            "-e", "ansible_remote_tmp=/tmp/ansible_tmp ansible_ssh_common_args='-o StrictHostKeyChecking=no'"
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=1800, cwd=cwd)
        success = result.returncode == 0
        return {
            "execution_id": execution_id,
            "success": success,
            "exit_code": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr
        }
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=408, detail="Playbook timed out after 30 minutes")
    except Exception as e:
        raise

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.post("/run-jcl")
async def run_jcl(playbook_name: str = "create_hamlet_jcl.yml", jcl_file: str = "GENER3", s3_prefix: str = ""):
    job_id = str(uuid.uuid4())[:8]
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            # Download playbook
            playbook_s3_path = f"{s3_prefix}ansible/{playbook_name}".lstrip("/")
            playbook_local = os.path.join(tmpdir, os.path.basename(playbook_name))
            download_from_s3(S3_BUCKET, playbook_s3_path, playbook_local)

            # Download JCL
            jcl_s3_path = f"{s3_prefix}jcl/{jcl_file}".lstrip("/")
            jcl_local = os.path.join(tmpdir, jcl_file)
            download_from_s3(S3_BUCKET, jcl_s3_path, jcl_local)

            # Download private key
            key_s3_path = "mainframe_key.pem"
            local_key_path = os.path.join(tmpdir, "mainframe_key.pem")
            download_from_s3(S3_BUCKET, key_s3_path, local_key_path)
            os.chmod(local_key_path, stat.S_IRUSR | stat.S_IWUSR)

            # Generate inventory
            inventory_dict = {
                'all': {
                    'children': {
                        'zos': {
                            'hosts': {
                                'mainframe': {
                                    'ansible_host': '67.217.62.83',
                                    'ansible_user': 'GAMA12',
                                    'ansible_python_interpreter': '/usr/lpp/IBM/cyp/v3r11/pyz/bin/python',
                                    'ansible_ssh_private_key_file': local_key_path
                                }
                            }
                        }
                    }
                }
            }
            inventory_path = os.path.join(tmpdir, 'inventory.yml')
            with open(inventory_path, 'w') as f:
                yaml.dump(inventory_dict, f)

            # Copy JCL locally to ../jcl
            target_dir = os.path.join(os.getcwd(), "../jcl")
            os.makedirs(target_dir, exist_ok=True)
            target_path = os.path.join(target_dir, jcl_file)
            shutil.copy(jcl_local, target_path)

            # Determine absolute directory containing the JCL file
            target_dir_abs = os.path.dirname(os.path.abspath(target_path))
            # e.g., "/home/ec2-user/ansible-zos/jcl"

            # Upload JCL to mainframe
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(hostname='67.217.62.83', username='GAMA12', key_filename=local_key_path)
            ssh.exec_command(f'mkdir -p /tmp/jcl')
            sftp = ssh.open_sftp()
            sftp.put(jcl_local, f"/tmp/jcl/{jcl_file}")
            sftp.close()
            ssh.close()

            # Run the playbook with cwd set to the directory containing the JCL
            execution_result = await execute_ansible_playbook(
                playbook_local,
                inventory_path,
                cwd=target_dir_abs  # <-- Important!
            )

            return {
                "job_id": job_id,
                "status": "completed",
                "playbook": playbook_name,
                "jcl_file": jcl_file,
                "s3_bucket": S3_BUCKET,
                "success": execution_result["success"],
                "exit_code": execution_result["exit_code"],
                "stdout": execution_result["stdout"],
                "stderr": execution_result["stderr"]
            }

    except Exception as e:
        # Log error and return failure
        return JSONResponse(
            status_code=500,
            content={"error": str(e), "job_id": job_id}
        )