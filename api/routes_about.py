"""Public About route, loaded by app.load_route_modules; no session or write key by design (D-05)."""
from . import about, db
from .app import app


@app.get("/api/about")
def about_view():
    with db.tx() as conn:
        return about.about(conn)
