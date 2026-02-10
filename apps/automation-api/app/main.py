from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
import uuid
import boto3
import subprocess
import tempfile
import os
import logging
import yaml
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

def is_collection_installed(collection_name):
    try:
        result = subprocess.run(
            ["ansible-galaxy", "collection", "list"],
            capture_output=True,
            text=True,
            timeout=60
        )
        output = result.stdout
        # Check if collection name appears in output
        return collection_name in output
    except Exception as e:
        logger.error(f"Error checking collections: {str(e)}")
        return False

def install_ansible_collections():
    collections = [
        "ibm.ibm_zos_core",
        "community.general",
        "ansible.posix"
    ]
    for collection in collections:
        try:
            if is_collection_installed(collection):
                logger.info(f"Collection '{collection}' already installed.")
                continue
            logger.info(f"Installing collection: {collection}")
            result = subprocess.run(
                ["ansible-galaxy", "collection", "install", collection, "--upgrade"],
                capture_output=True,
                text=True,
                timeout=300
            )
            logger.info(f"stdout: {result.stdout}")
            logger.info(f"stderr: {result.stderr}")
            if result.returncode == 0:
                logger.info(f"Successfully installed {collection}")
            else:
                logger.warning(f"Failed to install {collection}: {result.stderr}")
        except Exception as e:
            logger.error(f"Error installing {collection}: {str(e)}")

# Health Endpoint
@app.get("/health")
async def health():
    return {"status": "ok"}


@app.on_event("startup")
async def startup_event():
    """Install required Ansible collections on startup"""
    logger.info("Starting up - installing Ansible collections...")
    install_ansible_collections()
    logger.info("Startup complete - collections installed")


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


def render_jinja2_variables(variables: Dict) -> Dict:
    """
    Render Jinja2 templates in variable values using other variables in the dict.
    Handles nested dictionaries and lists recursively.
    """
    try:
        logger.info("Rendering Jinja2 templates in variables")
        
        # Create a Jinja2 environment
        jinja_env = Environment(loader=BaseLoader())
        
        def process_value(value, context):
            """Recursively process values to render Jinja2 expressions"""
            if isinstance(value, str):
                try:
                    # Check if string contains Jinja2 expressions
                    if "{{" in value or "{%" in value:
                        template = jinja_env.from_string(value)
                        rendered = template.render(context)
                        logger.debug(f"Rendered: {value} → {rendered}")
                        return rendered
                    return value
                except Exception as e:
                    logger.warning(f"Failed to render template '{value}': {str(e)}")
                    return value
            elif isinstance(value, dict):
                return {k: process_value(v, context) for k, v in value.items()}
            elif isinstance(value, list):
                return [process_value(item, context) for item in value]
            else:
                return value
        
        # First pass: create a context with all top-level variables
        rendered_vars = {}
        
        # Process all values, using accumulated context
        for key, value in variables.items():
            rendered_value = process_value(value, variables)
            rendered_vars[key] = rendered_value
        
        # Second pass: re-render to handle cross-references
        final_vars = {}
        for key, value in rendered_vars.items():
            final_value = process_value(value, rendered_vars)
            final_vars[key] = final_value
        
        logger.info(f"Successfully rendered Jinja2 templates for {len(final_vars)} variables")
        return final_vars
    except Exception as e:
        logger.error(f"Error rendering Jinja2 templates: {str(e)}")
        raise


def load_yaml_vars(yaml_file_path: str) -> Dict:
    """Load variables from a YAML file and render Jinja2 templates"""
    try:
        logger.info(f"Loading variables from YAML file: {yaml_file_path}")
        with open(yaml_file_path, 'r') as f:
            variables = yaml.safe_load(f)
        if variables is None:
            variables = {}
        
        logger.info(f"Loaded {len(variables)} variables from {yaml_file_path}")
        
        # Render Jinja2 templates in the variables
        rendered_variables = render_jinja2_variables(variables)
        
        logger.info(f"Successfully loaded and rendered {len(rendered_variables)} variables")
        return rendered_variables
    except Exception as e:
        logger.error(f"Error loading YAML file {yaml_file_path}: {str(e)}")
        raise


async def execute_ansible_playbook(
    playbook_path: str,
    inventory_path: str,
    extra_vars: Optional[Dict] = None
) -> Dict:
    """Execute Ansible playbook and capture output"""
    execution_id = str(uuid.uuid4())[:8]
    logger.info(f"[{execution_id}] Starting playbook execution")
    
    try:
        # Build ansible-playbook command
        command = [
            "ansible-playbook",
            "-i", inventory_path,
            playbook_path
        ]
        
        # Add extra vars if provided
        if extra_vars:
            for key, value in extra_vars.items():
                command.extend(["-e", f"{key}={value}"])
        
        logger.info(f"[{execution_id}] Running command: {' '.join(command)}")
        
        # Execute the playbook
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=1800  # 30 minute timeout
        )
        
        success = result.returncode == 0
        logger.info(f"[{execution_id}] Playbook execution completed with exit code: {result.returncode}")
        
        return {
            "execution_id": execution_id,
            "success": success,
            "exit_code": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr
        }
    
    except subprocess.TimeoutExpired:
        logger.error(f"[{execution_id}] Playbook execution timed out")
        raise HTTPException(
            status_code=408,
            detail="Playbook execution timed out after 30 minutes"
        )
    except Exception as e:
        logger.error(f"[{execution_id}] Error executing playbook: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Playbook execution failed: {str(e)}")

# JCL Endpoint
@app.post("/run-jcl")
async def run_jcl(
    playbook_name: str = "ansible/create_hamlet_jcl.yml",
    inventory_name: str = "inventory.yml",
    jcl_file: str = "GENER3",
    s3_prefix: str = "",
    extra_vars: Optional[Dict] = None
):
    """
    Run an Ansible playbook downloaded from S3, along with JCL file and variables
    
    Parameters:
    - playbook_name: Name of the playbook file in S3 (e.g., ansible/create_hamlet_jcl.yml)
    - inventory_name: Name of the inventory file in S3 (e.g., inventory.yml)
    - jcl_file: JCL file name in jcl/ folder (e.g., GENER3)
    - s3_prefix: S3 path prefix (e.g., "ansible-files/") - optional
    - extra_vars: Extra variables to pass to ansible-playbook (e.g., {"target_host": "zos1"})
    
    Note: Automatically loads var.yml from S3 bucket root if it exists.
    Variables from var.yml are passed to the playbook as environment_vars and other variables.
    extra_vars parameter takes precedence over var.yml values.
    """
    job_id = str(uuid.uuid4())[:8]
    
    try:
        # Create temporary directory for downloaded files
        with tempfile.TemporaryDirectory() as tmpdir:
            logger.info(f"[{job_id}] Created temporary directory: {tmpdir}")
            
            # Create subdirectories to maintain structure
            jcl_tmpdir = os.path.join(tmpdir, "jcl")
            os.makedirs(jcl_tmpdir, exist_ok=True)
            
            # Construct S3 paths
            playbook_s3_path = f"{s3_prefix}{playbook_name}".lstrip("/")
            inventory_s3_path = f"{s3_prefix}{inventory_name}".lstrip("/")
            jcl_s3_path = f"{s3_prefix}jcl/{jcl_file}".lstrip("/")
            
            # Local paths
            playbook_local = os.path.join(tmpdir, os.path.basename(playbook_name))
            inventory_local = os.path.join(tmpdir, inventory_name)
            jcl_local = os.path.join(jcl_tmpdir, jcl_file)
            
            # Download playbook from S3
            logger.info(f"[{job_id}] Downloading playbook from S3: {playbook_s3_path}")
            download_from_s3(S3_BUCKET, playbook_s3_path, playbook_local)
            
            # Download inventory from S3
            logger.info(f"[{job_id}] Downloading inventory from S3: {inventory_s3_path}")
            download_from_s3(S3_BUCKET, inventory_s3_path, inventory_local)
            
            # Download JCL file from S3
            logger.info(f"[{job_id}] Downloading JCL file from S3: {jcl_s3_path}")
            download_from_s3(S3_BUCKET, jcl_s3_path, jcl_local)
            
            # Download var.yml from S3 if it exists
            var_s3_path = f"{s3_prefix}var.yml".lstrip("/")
            var_local = os.path.join(tmpdir, "var.yml")
            var_data = {}
            
            try:
                logger.info(f"[{job_id}] Downloading variables from S3: {var_s3_path}")
                download_from_s3(S3_BUCKET, var_s3_path, var_local)
                var_data = load_yaml_vars(var_local)
                logger.info(f"[{job_id}] Loaded variables from var.yml: {list(var_data.keys())}")
            except Exception as e:
                logger.warning(f"[{job_id}] Could not load var.yml (optional): {str(e)}")
            
            # Merge var.yml variables with extra_vars (extra_vars takes precedence)
            if extra_vars is None:
                extra_vars = {}
            merged_vars = {**var_data, **extra_vars}
            merged_vars["jcl_file"] = jcl_local
            
            logger.info(f"[{job_id}] Final variables: {list(merged_vars.keys())}")
            
            # Execute ansible-playbook
            logger.info(f"[{job_id}] Executing ansible-playbook")
            execution_result = await execute_ansible_playbook(
                playbook_local,
                inventory_local,
                merged_vars
            )
            
            return JSONResponse({
                "job_id": job_id,
                "status": "completed",
                "playbook": playbook_name,
                "inventory": inventory_name,
                "jcl_file": jcl_file,
                "jcl_local_path": jcl_local,
                "s3_bucket": S3_BUCKET,
                "variables_loaded": list(var_data.keys()),
                "variables_count": len(merged_vars),
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