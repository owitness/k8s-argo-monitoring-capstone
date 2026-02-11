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
from pathlib import Path

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

def _s3_key(prefix: str, *parts: str) -> str:
    """Build S3 key with proper slash handling between prefix and parts."""
    path = "/".join(parts)
    if not prefix:
        return path.lstrip("/")
    prefix = prefix.rstrip("/")
    return f"{prefix}/{path}".lstrip("/")

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

async def execute_ansible_playbook(
    playbook_path: str,
    inventory_path: str,
    tmpdir: str,
    jcl_file: str = "GENER3",
) -> dict:
    """
    Execute Ansible playbook in tmpdir with cwd set to tmpdir
    """
    execution_id = str(uuid.uuid4())[:8]
    logger.info(f"[{execution_id}] Starting playbook execution in {tmpdir}")
    try:
        # Create ansible.cfg in tmpdir
        ansible_cfg_path = os.path.join(tmpdir, 'ansible.cfg')
        with open(ansible_cfg_path, 'w') as f:
            f.write("""[defaults]
forks = 25
environment = LANG=en_US.UTF-8,LC_ALL=en_US.UTF-8
host_key_checking = False

[ssh_connection]
pipelining = True
""")
        # Run the ansible-playbook command with cwd=tmpdir
        # Build extra vars: pass jcl_file so playbook can use {{ playbook_dir }}/{{ jcl_file }}
        extra_vars = f"ansible_remote_tmp=/tmp/ansible-gama12 ansible_ssh_common_args='-o StrictHostKeyChecking=no' jcl_file={jcl_file}"
        command = [
            "ansible-playbook",
            "-i", inventory_path,
            playbook_path,
            "-e", extra_vars
        ]
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=1800,
            cwd=tmpdir  # Important: set cwd here
        )
        success = result.returncode == 0
        logger.info(f"[{execution_id}] Playbook completed with code {result.returncode}")
        return {
            "execution_id": execution_id,
            "success": success,
            "exit_code": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr
        }
    except subprocess.TimeoutExpired:
        logger.error(f"[{execution_id}] Playbook timed out")
        raise HTTPException(
            status_code=408,
            detail="Playbook timed out after 30 minutes"
        )
    except Exception as e:
        logger.error(f"[{execution_id}] Error executing playbook: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Playbook execution failed: {str(e)}")


@app.post("/run-jcl")
async def run_jcl(
    playbook_name: str = "create_hamlet_jcl.yml",
    #inventory_name: str = "inventory.yml",
    jcl_file: str = "GENER3",
    s3_prefix: str = "",
):
    job_id = str(uuid.uuid4())[:8]
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            logger.info(f"[{job_id}] Created temporary directory: {tmpdir}")

            # Download playbook
            playbook_s3_path = _s3_key(s3_prefix, "ansible", playbook_name)
            playbook_local = os.path.join(tmpdir, os.path.basename(playbook_name))
            download_from_s3(S3_BUCKET, playbook_s3_path, playbook_local)

            # Download the JCL file from S3
            jcl_s3_path = _s3_key(s3_prefix, "jcl", jcl_file)
            jcl_local = os.path.join(tmpdir, jcl_file)
            download_from_s3(S3_BUCKET, jcl_s3_path, jcl_local)
            logger.info(f"[{job_id}] Downloaded JCL to {jcl_local}")

            # Download the private key
            key_s3_path = "mainframe_key.pem"  # Adjust as needed
            local_key_path = os.path.join(tmpdir, "mainframe_key.pem")
            download_from_s3(S3_BUCKET, key_s3_path, local_key_path)
            os.chmod(local_key_path, stat.S_IRUSR | stat.S_IWUSR)

            # Build inventory (key from S3 at local_key_path, user GAMA12)
            inventory_dict = {
                'all': {
                    'children': {
                        'zos': {
                            'hosts': {
                                'mainframe': {
                                    'ansible_host': '67.217.62.83',
                                    'ansible_user': 'GAMA12',
                                    'ansible_connection': 'ssh',
                                    'ansible_ssh_private_key_file': local_key_path,
                                    'ansible_ssh_common_args': '-o StrictHostKeyChecking=no',
                                    'ansible_python_interpreter': '/usr/lpp/IBM/cyp/v3r11/pyz/bin/python3',
                                    'ansible_pipelining': True,
                                    'ansible_remote_tmp': '/tmp/ansible-gama12'
                                }
                            }
                        }
                    }
                }
            }
            inventory_path = os.path.join(tmpdir, 'inventory.yml')
            with open(inventory_path, 'w') as f:
                yaml.dump(inventory_dict, f)

            # Prepare target directory for JCL
            target_dir = os.path.join(os.getcwd(), "../jcl")
            os.makedirs(target_dir, exist_ok=True)
            target_path = os.path.join(target_dir, jcl_file)
            shutil.copy(jcl_local, target_path)
            logger.info(f"[{job_id}] Copied JCL to {target_path}")

            # Run ansible playbook with cwd=tmpdir so relative paths work
            logger.info(f"[{job_id}] Executing ansible-playbook")
            execution_result = await execute_ansible_playbook(
                playbook_local,
                inventory_path,
                tmpdir,
                jcl_file=jcl_file,
            )

            # Return the result
            return JSONResponse({
                "job_id": job_id,
                "status": "completed",
                "playbook": playbook_name,
                "jcl_file": jcl_file,
                "jcl_local_path": jcl_local,
                "s3_bucket": S3_BUCKET,
                "success": execution_result["success"],
                "exit_code": execution_result["exit_code"],
                "execution_id": execution_result["execution_id"],
                "stdout": execution_result["stdout"],
                "stderr": execution_result["stderr"]
            })

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[{job_id}] Error in run-jcl: {str(e)}")
        return JSONResponse(
            status_code=500,
            content={
                "job_id": job_id,
                "status": "failed",
                "error": str(e)
            }
        )