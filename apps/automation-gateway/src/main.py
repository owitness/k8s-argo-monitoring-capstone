"""
"""

import logging
import os
import subprocess
from typing import Optional, Dict

from fastapi import FastAPI, HTTPException
from prometheus_fastapi_instrumentator import Instrumentator
from prometheus_client import Counter, Histogram, Gauge
import uuid
from datetime import datetime
from pydantic import BaseModel, Field

# This is a test
# ---------------------------------------------------------------------------
# Configuration and Setup
# ---------------------------------------------------------------------------

# Ansible working directory
ANSIBLE_BASE_DIR = "/home/ec2-user/ansible-zos"
ANSIBLE_DIR = os.path.join(ANSIBLE_BASE_DIR, "ansible")

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("automation-gateway")

# Prometheus metrics for Ansible playbooks
playbook_executions_total = Counter(
    "playbook_executions_total",
    "Total number of playbook executions",
    ["playbook", "status"]
)

playbook_execution_duration_seconds = Histogram(
    "playbook_execution_duration_seconds",
    "Duration of playbook execution in seconds",
    ["playbook"]
)

playbook_queue_depth = Gauge(
    "playbook_queue_depth",
    "Number of playbooks currently executing or queued"
)

# Request/Response models
class PlaybookExecutionRequest(BaseModel):
    """Request to execute an Ansible playbook"""
    playbook: str = Field(
        default="create_hamlet_jcl.yml",
        description="Name of the playbook file to execute"
    )
    inventory: str = Field(
        default="inventory.yml",
        description="Inventory file to use"
    )
    extra_vars: Optional[Dict] = Field(
        default=None,
        description="Extra variables to pass to the playbook"
    )

class PlaybookExecutionResponse(BaseModel):
    """Response from playbook execution"""
    execution_id: str
    playbook: str
    success: bool
    exit_code: int
    stdout: str
    stderr: str
    started_at: str
    finished_at: str
    duration_seconds: float

class HealthResponse(BaseModel):
    """Health check response"""
    status: str
    ansible_dir: str
    ansible_available: bool


# ---------------------------------------------------------------------------
# Playbook Execution Logic
# ---------------------------------------------------------------------------

async def execute_playbook(request: PlaybookExecutionRequest) -> PlaybookExecutionResponse:
    """Execute an Ansible playbook and capture output"""
    execution_id = str(uuid.uuid4())
    started_at = datetime.utcnow().isoformat()
    
    playbook_queue_depth.inc()
    
    try:
        # Validate playbook exists
        playbook_path = os.path.join(ANSIBLE_DIR, request.playbook)
        if not os.path.exists(playbook_path):
            raise ValueError(f"Playbook '{request.playbook}' not found in {ANSIBLE_DIR}")
        
        # Validate inventory exists
        inventory_path = os.path.join(ANSIBLE_DIR, request.inventory)
        if not os.path.exists(inventory_path):
            raise ValueError(f"Inventory '{request.inventory}' not found in {ANSIBLE_DIR}")
        
        # Build command
        command = [
            "ansible-playbook",
            "-i", request.inventory,
            request.playbook
        ]
        
        # Add extra vars if provided
        if request.extra_vars:
            extra_vars_str = " ".join([f"{k}={v}" for k, v in request.extra_vars.items()])
            command.extend(["-e", extra_vars_str])
        
        logger.info(f"[{execution_id}] Running playbook: {request.playbook} with inventory: {request.inventory}")
        logger.debug(f"[{execution_id}] Command: {' '.join(command)}")
        
        # Execute playbook
        result = subprocess.run(
            command,
            cwd=ANSIBLE_DIR,
            capture_output=True,
            text=True,
            timeout=1800  # 30 minute timeout
        )
        
        finished_at = datetime.utcnow().isoformat()
        success = result.returncode == 0
        duration = (datetime.fromisoformat(finished_at) - datetime.fromisoformat(started_at)).total_seconds()
        
        # Record metrics
        status = "success" if success else "failed"
        playbook_executions_total.labels(playbook=request.playbook, status=status).inc()
        playbook_execution_duration_seconds.labels(playbook=request.playbook).observe(duration)
        
        logger.info(f"[{execution_id}] Playbook execution completed with status: {status} (duration: {duration:.2f}s)")
        
        return PlaybookExecutionResponse(
            execution_id=execution_id,
            playbook=request.playbook,
            success=success,
            exit_code=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            started_at=started_at,
            finished_at=finished_at,
            duration_seconds=duration
        )
    
    except subprocess.TimeoutExpired:
        logger.error(f"[{execution_id}] Playbook execution timed out after 30 minutes")
        raise ValueError("Playbook execution timed out after 30 minutes")
    except Exception as e:
        logger.error(f"[{execution_id}] Error executing playbook: {str(e)}")
        raise
    finally:
        playbook_queue_depth.dec()

# ---------------------------------------------------------------------------
# FastAPI Application Setup
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Ansible Automation Gateway",
    description="FastAPI service for executing Ansible playbooks with OpenTelemetry instrumentation",
    version="1.0.0"
)

# Prometheus metrics instrumentation
Instrumentator().instrument(app).expose(app, endpoint="/metrics")


# ---------------------------------------------------------------------------
# Startup Events
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup():
    """Initialize and validate Ansible environment"""
    logger.info("Starting Automation Gateway...")
    
    # Check Ansible directory
    if not os.path.isdir(ANSIBLE_DIR):
        logger.warning(f"Ansible directory not found: {ANSIBLE_DIR}")
    else:
        logger.info(f"Ansible directory: {ANSIBLE_DIR}")
        playbooks = [f for f in os.listdir(ANSIBLE_DIR) if f.endswith('.yml')]
        logger.info(f"Found {len(playbooks)} playbooks")
    
    # Check ansible-playbook command availability
    try:
        result = subprocess.run(
            ["ansible-playbook", "--version"],
            capture_output=True,
            text=True
        )
        if result.returncode == 0:
            logger.info("ansible-playbook is available")
        else:
            logger.warning("ansible-playbook command failed")
    except FileNotFoundError:
        logger.warning("ansible-playbook command not found in PATH")
    
    logger.info("Automation Gateway startup complete")


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------

@app.get("/health", response_model=HealthResponse)
async def health_check():
    """Health check endpoint with Ansible availability status"""
    ansible_available = False
    try:
        result = subprocess.run(
            ["ansible-playbook", "--version"],
            capture_output=True,
            timeout=5
        )
        ansible_available = result.returncode == 0
    except:
        ansible_available = False
    
    return HealthResponse(
        status="healthy",
        ansible_dir=ANSIBLE_DIR,
        ansible_available=ansible_available
    )


@app.post("/playbooks/execute", response_model=PlaybookExecutionResponse)
async def execute(request: PlaybookExecutionRequest = PlaybookExecutionRequest()):
    """
    Execute an Ansible playbook and return the output.
    
    Default execution: POST /playbooks/execute with empty body executes create_hamlet_jcl.yml
    """
    try:
        response = await execute_playbook(request)
        return response
    except ValueError as e:
        logger.error(f"Validation error: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Execution error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error executing playbook: {str(e)}")


@app.get("/playbooks/available")
async def list_available_playbooks():
    """List all available playbooks in the Ansible directory"""
    try:
        if not os.path.isdir(ANSIBLE_DIR):
            raise HTTPException(
                status_code=404,
                detail=f"Ansible directory not found: {ANSIBLE_DIR}"
            )
        
        playbooks = [f for f in os.listdir(ANSIBLE_DIR) if f.endswith('.yml')]
        return {
            "directory": ANSIBLE_DIR,
            "count": len(playbooks),
            "playbooks": sorted(playbooks)
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing playbooks: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error listing playbooks: {str(e)}")


@app.get("/playbooks/{playbook_name}")
async def get_playbook_info(playbook_name: str):
    """Get information about a specific playbook"""
    playbook_path = os.path.join(ANSIBLE_DIR, playbook_name)
    
    if not os.path.exists(playbook_path):
        raise HTTPException(
            status_code=404,
            detail=f"Playbook '{playbook_name}' not found"
        )
    
    try:
        with open(playbook_path, 'r') as f:
            content = f.read()
        
        return {
            "name": playbook_name,
            "path": playbook_path,
            "exists": True,
            "size_bytes": os.path.getsize(playbook_path),
            "content": content[:1000] + "..." if len(content) > 1000 else content
        }
    except Exception as e:
        logger.error(f"Error reading playbook: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error reading playbook: {str(e)}")


@app.get("/")
async def root():
    """Welcome endpoint with API documentation links"""
    return {
        "service": "Ansible Automation Gateway",
        "version": "1.0.0",
        "documentation": "/docs",
        "redoc": "/redoc",
        "metrics": "/metrics",
        "endpoints": {
            "health": "GET /health",
            "execute_playbook": "POST /playbooks/execute",
            "list_playbooks": "GET /playbooks/available",
            "playbook_info": "GET /playbooks/{playbook_name}"
        }
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)