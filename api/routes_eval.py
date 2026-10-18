"""The read-only, signed-out AI evaluation summary (10-CONTEXT D-16)."""
from importlib import import_module

from agent.eval import store
from . import db
from .app import app


def current_identity():
    """Only model and assistant release identity; loading settings makes no provider request."""
    try:
        config = import_module("agent.config")
    except ModuleNotFoundError as exc:
        if exc.name != "agent.config":
            raise
        return None
    return {"model_id": config.model_id(), "assistant_version": config.ASSISTANT_VERSION}


@app.get("/api/eval/summary")
def eval_summary():
    with db.tx() as conn:
        return store.public_view(conn, current_identity())
