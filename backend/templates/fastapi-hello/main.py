"""FastAPI Hello starter for Zeni Cloud Run.

Auto Swagger UI at /docs, ReDoc at /redoc.
"""
import os
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

app = FastAPI(
    title="FastAPI on Zeni Cloud",
    description="Hello-world starter — replace endpoints to ship your API.",
    version="1.0.0",
)


@app.get("/", response_class=HTMLResponse)
def root() -> str:
    return """
    <html><head><title>FastAPI on Zeni Cloud</title>
    <meta name='viewport' content='width=device-width,initial-scale=1'/></head>
    <body style='margin:0;font-family:system-ui;background:linear-gradient(135deg,#0f0f23,#1a1a3e);color:#fff;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px'>
    <div style='max-width:720px;text-align:center'>
        <div style='font-size:64px'>FastAPI</div>
        <h1 style='color:#22d3ee;font-size:36px;margin:8px 0'>Welcome to FastAPI on Zeni Cloud</h1>
        <p style='color:#cbd5e1'>Your Python API is live. Try the auto-generated docs.</p>
        <ul style='list-style:none;padding:0;text-align:left;background:rgba(255,255,255,0.04);border:1px solid rgba(34,211,238,0.3);border-radius:12px;padding:24px'>
            <li style='padding:6px 0'>Auto-generated Swagger UI at <a href='/docs' style='color:#22d3ee'>/docs</a></li>
            <li style='padding:6px 0'>ReDoc at <a href='/redoc' style='color:#22d3ee'>/redoc</a></li>
            <li style='padding:6px 0'>Health check at <a href='/health' style='color:#22d3ee'>/health</a></li>
            <li style='padding:6px 0'>Async by default — uvicorn ASGI</li>
            <li style='padding:6px 0'>Edit main.py and redeploy</li>
        </ul>
        <p style='font-size:12px;color:#64748b;margin-top:24px'>Powered by <strong style='color:#22d3ee'>Zeni Cloud</strong> · FastAPI</p>
    </div></body></html>
    """


@app.get("/api")
def api_info() -> dict:
    return {
        "service": "FastAPI on Zeni Cloud",
        "status": "ok",
        "version": "1.0.0",
        "docs": "/docs",
    }


@app.get("/health")
def health() -> dict:
    return {"status": "healthy"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
