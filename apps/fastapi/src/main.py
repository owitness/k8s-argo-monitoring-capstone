"""
FastAPI application with OpenTelemetry auto-instrumentation support.

Run with: opentelemetry-instrument uvicorn main:app --host 0.0.0.0 --port 8000
"""

import logging
import os
import random
import time
from typing import Optional

import redis
from fastapi import FastAPI, HTTPException
from prometheus_fastapi_instrumentator import Instrumentator
from pydantic_settings import BaseSettings
from sqlalchemy import Column, Integer, String, create_engine
from sqlalchemy.orm import sessionmaker, declarative_base


# ---------------------------------------------------------------------------
# Configuration via Environment Variables
# ---------------------------------------------------------------------------

class Settings(BaseSettings):
    """Application settings loaded from environment variables."""
    
    # MySQL
    mysql_host: str = "mysql"
    mysql_port: int = 3306
    mysql_user: str = "root"
    mysql_password: str = "rootpassword"
    mysql_database: str = "testdb"
    
    # Redis
    redis_host: str = "redis"
    redis_port: int = 6379
    redis_password: Optional[str] = None
    
    @property
    def database_url(self) -> str:
        return f"mysql+pymysql://{self.mysql_user}:{self.mysql_password}@{self.mysql_host}:{self.mysql_port}/{self.mysql_database}"
    
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()


# ---------------------------------------------------------------------------
# Logging Configuration (OTel-safe)
# ---------------------------------------------------------------------------

class OTelSafeFormatter(logging.Formatter):
    """Formatter that safely includes OTel trace context if available."""
    
    def format(self, record):
        # Add default values for OTel fields if not present
        if not hasattr(record, 'otelTraceID'):
            record.otelTraceID = '0'
        if not hasattr(record, 'otelSpanID'):
            record.otelSpanID = '0'
        return super().format(record)

# Configure logging
handler = logging.StreamHandler()
handler.setFormatter(OTelSafeFormatter(
    '%(asctime)s - %(levelname)s - %(name)s - [trace_id=%(otelTraceID)s span_id=%(otelSpanID)s] - %(message)s'
))

logging.basicConfig(level=logging.INFO, handlers=[handler])
logger = logging.getLogger("fastapi-app")

# ---------------------------------------------------------------------------
# MySQL Configuration
# ---------------------------------------------------------------------------

engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class Item(Base):
    __tablename__ = "items"
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    description = Column(String(500))


# ---------------------------------------------------------------------------
# Redis Configuration
# ---------------------------------------------------------------------------

def get_redis_client() -> redis.Redis:
    """Create Redis client with settings."""
    return redis.Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        password=settings.redis_password or None,
        decode_responses=True
    )


# ---------------------------------------------------------------------------
# FastAPI Application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Telemetry API",
    description="FastAPI with OpenTelemetry auto-instrumentation",
    version="1.0.0"
)

# Prometheus metrics
Instrumentator().instrument(app).expose(app, endpoint="/metrics")


# ---------------------------------------------------------------------------
# Startup Events
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup():
    """Initialize database and test connections."""
    logger.info("Starting application...")
    
    # Test MySQL connection and create tables if available
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("MySQL tables created")
        with engine.connect() as conn:
            conn.exec_driver_sql("SELECT 1")
        logger.info("MySQL connection successful")
    except Exception as e:
        logger.warning(f"MySQL not available at startup (will retry on requests): {e}")
    
    # Test Redis connection
    try:
        r = get_redis_client()
        r.ping()
        logger.info("Redis connection successful")
    except Exception as e:
        logger.warning(f"Redis not available at startup (will retry on requests): {e}")
    
    logger.info("Application started - endpoints ready")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/")
def root():
    """Health check endpoint."""
    logger.info("Health check requested")
    return {"status": "healthy", "service": "fastapi-app"}


@app.get("/health")
def health():
    """Detailed health check."""
    # Check MySQL
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql("SELECT 1")
        mysql_status = "connected"
    except Exception:
        mysql_status = "disconnected"
    
    # Check Redis
    try:
        r = get_redis_client()
        r.ping()
        redis_status = "connected"
    except Exception:
        redis_status = "disconnected"
    
    return {
        "status": "healthy",
        "mysql": mysql_status,
        "redis": redis_status
    }


@app.get("/items")
def get_items():
    """Get all items."""
    logger.info("Fetching all items")
    try:
        db = SessionLocal()
        try:
            items = db.query(Item).all()
            return {"items": [{"id": i.id, "name": i.name, "description": i.description} for i in items]}
        finally:
            db.close()
    except Exception as e:
        logger.error(f"MySQL error: {e}")
        raise HTTPException(status_code=503, detail="MySQL unavailable")


@app.get("/items/{item_id}")
def get_item(item_id: int):
    """Get a single item by ID."""
    logger.debug(f"Looking up item: {item_id}")
    try:
        db = SessionLocal()
        try:
            item = db.query(Item).filter(Item.id == item_id).first()
            if not item:
                logger.warning(f"Item not found: {item_id}")
                raise HTTPException(status_code=404, detail="Item not found")
            return {"id": item.id, "name": item.name, "description": item.description}
        finally:
            db.close()
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"MySQL error: {e}")
        raise HTTPException(status_code=503, detail="MySQL unavailable")


@app.post("/items")
def create_item(name: str, description: str = ""):
    """Create a new item."""
    logger.info(f"Creating item: {name}")
    try:
        db = SessionLocal()
        try:
            item = Item(name=name, description=description)
            db.add(item)
            db.commit()
            db.refresh(item)
            return {"id": item.id, "name": item.name, "description": item.description}
        finally:
            db.close()
    except Exception as e:
        logger.error(f"MySQL error: {e}")
        raise HTTPException(status_code=503, detail="MySQL unavailable")


@app.get("/slow")
def slow_endpoint():
    """Intentionally slow endpoint for testing latency."""
    delay = random.uniform(1.0, 3.0)
    logger.warning(f"Slow endpoint called, sleeping {delay:.2f}s")
    time.sleep(delay)
    return {"message": "Done", "delay": f"{delay:.2f}s"}


@app.get("/error")
def error_endpoint():
    """Always fails - for testing error tracking."""
    logger.error("Error endpoint called - intentional failure")
    raise HTTPException(status_code=500, detail="Intentional error")


@app.get("/random")
def random_endpoint():
    """Generate random log levels."""
    level = random.choice(["debug", "info", "warning", "error"])
    
    if level == "debug":
        logger.debug("Random debug message")
    elif level == "info":
        logger.info("Random info message")
    elif level == "warning":
        logger.warning("Random warning message")
    else:
        logger.error("Random error message")
    
    return {"log_level": level}


# ---------------------------------------------------------------------------
# Redis Endpoints
# ---------------------------------------------------------------------------

@app.get("/cache/{key}")
def cache_get(key: str):
    """Get a value from Redis cache."""
    logger.info(f"Cache GET: {key}")
    try:
        r = get_redis_client()
        value = r.get(key)
        if value is None:
            logger.debug(f"Cache MISS: {key}")
            raise HTTPException(status_code=404, detail="Key not found")
        logger.debug(f"Cache HIT: {key}")
        return {"key": key, "value": value}
    except HTTPException:
        raise
    except (redis.RedisError, redis.ConnectionError) as e:
        logger.error(f"Redis unavailable: {e}")
        raise HTTPException(status_code=503, detail="Redis unavailable")


@app.post("/cache/{key}")
def cache_set(key: str, value: str, ttl: int = 300):
    """Set a value in Redis cache with optional TTL (default 5 minutes)."""
    logger.info(f"Cache SET: {key}={value} (TTL: {ttl}s)")
    try:
        r = get_redis_client()
        r.setex(key, ttl, value)
        return {"key": key, "value": value, "ttl": ttl}
    except (redis.RedisError, redis.ConnectionError) as e:
        logger.error(f"Redis unavailable: {e}")
        raise HTTPException(status_code=503, detail="Redis unavailable")


@app.delete("/cache/{key}")
def cache_delete(key: str):
    """Delete a key from Redis cache."""
    logger.info(f"Cache DELETE: {key}")
    try:
        r = get_redis_client()
        deleted = r.delete(key)
        if deleted == 0:
            raise HTTPException(status_code=404, detail="Key not found")
        return {"deleted": key}
    except HTTPException:
        raise
    except (redis.RedisError, redis.ConnectionError) as e:
        logger.error(f"Redis unavailable: {e}")
        raise HTTPException(status_code=503, detail="Redis unavailable")


@app.post("/cache/counter/{key}")
def cache_increment(key: str, amount: int = 1):
    """Increment a counter in Redis."""
    logger.info(f"Cache INCR: {key} by {amount}")
    try:
        r = get_redis_client()
        new_value = r.incrby(key, amount)
        return {"key": key, "value": new_value}
    except (redis.RedisError, redis.ConnectionError) as e:
        logger.error(f"Redis unavailable: {e}")
        raise HTTPException(status_code=503, detail="Redis unavailable")


@app.get("/cache/stats")
def cache_stats():
    """Get Redis server stats."""
    logger.info("Fetching Redis stats")
    try:
        r = get_redis_client()
        info = r.info("stats")
        return {
            "total_connections": info.get("total_connections_received", 0),
            "total_commands": info.get("total_commands_processed", 0),
            "keyspace_hits": info.get("keyspace_hits", 0),
            "keyspace_misses": info.get("keyspace_misses", 0),
            "hit_rate": round(
                info.get("keyspace_hits", 0) / 
                max(info.get("keyspace_hits", 0) + info.get("keyspace_misses", 0), 1) * 100, 2
            )
        }
    except (redis.RedisError, redis.ConnectionError) as e:
        logger.error(f"Redis unavailable: {e}")
        raise HTTPException(status_code=503, detail="Redis unavailable")
