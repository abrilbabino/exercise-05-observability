import time
from datetime import datetime, timezone
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from sqlalchemy import text
from sqlalchemy.orm import Session
from src.database import Base, engine, get_db
from src.models import Node
from src.schemas import NodeCreate, NodeResponse, NodeUpdate
from prometheus_client import make_asgi_app, Counter, Gauge, Histogram

Base.metadata.create_all(bind=engine)
app = FastAPI()

# Metrics
registry_requests_total = Counter(
    "noderegistry_requests_total",
    "Total requests received by the node registry API",
    ["method", "path", "status_code"],
)
registry_request_duration_seconds = Histogram(
    "noderegistry_request_duration_seconds",
    "Request processing time in seconds",
    ["method", "path"],
)
registry_active_nodes = Gauge(
    "noderegistry_active_nodes_total",
    "Number of nodes currently in active state",
)

class MetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        start_time = time.time()
        response = await call_next(request)
        process_time = time.time() - start_time
        
        request_path = request.url.path
        if request_path != "/metrics":
            registry_requests_total.labels(
                method=request.method,
                path=request_path,
                status_code=str(response.status_code),
            ).inc()
            registry_request_duration_seconds.labels(
                method=request.method,
                path=request_path,
            ).observe(process_time)
        return response

app.add_middleware(MetricsMiddleware)

metrics_app = make_asgi_app()
app.mount("/metrics", metrics_app)

@app.get("/health")
def health(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
        db_status = "connected"
    except Exception:
        db_status = "disconnected"
    count = db.query(Node).filter(Node.status == "active").count()

    registry_active_nodes.set(count)

    return {"status": "ok", "db": db_status, "nodes_count": count}

@app.post("/api/nodes", response_model=NodeResponse, status_code=201)
def register_node(node: NodeCreate, db: Session = Depends(get_db)):
    existing = db.query(Node).filter(Node.name == node.name).first()
    if existing:
        raise HTTPException(status_code=409, detail="Node already exists")
    db_node = Node(name=node.name, host=node.host, port=node.port)
    db.add(db_node)
    db.commit()
    db.refresh(db_node)

    active_count = db.query(Node).filter(Node.status == "active").count()
    registry_active_nodes.set(active_count)

    return db_node

@app.get("/api/nodes", response_model=list[NodeResponse])
def list_nodes(db: Session = Depends(get_db)):
    return db.query(Node).all()

@app.get("/api/nodes/{name}", response_model=NodeResponse)
def get_node(name: str, db: Session = Depends(get_db)):
    node = db.query(Node).filter(Node.name == name).first()
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    return node

@app.put("/api/nodes/{name}", response_model=NodeResponse)
def update_node(name: str, update: NodeUpdate, db: Session = Depends(get_db)):
    node = db.query(Node).filter(Node.name == name).first()
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    if update.host is not None:
        node.host = update.host
    if update.port is not None:
        node.port = update.port
    node.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(node)
    return node

@app.delete("/api/nodes/{name}", status_code=204)
def delete_node(name: str, db: Session = Depends(get_db)):
    node = db.query(Node).filter(Node.name == name).first()
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    node.status = "inactive"
    node.updated_at = datetime.now(timezone.utc)
    db.commit()

    active_count = db.query(Node).filter(Node.status == "active").count()
    registry_active_nodes.set(active_count)

    return Response(status_code=204)
