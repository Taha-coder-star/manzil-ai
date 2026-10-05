from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app import health
from app.config import load_settings

app = FastAPI(title="Manzil AI")


@app.get("/live")
def live():
    return {"status": "running", "role": "web"}


@app.get("/health")
def health_check():
    settings = load_settings()
    checks = {
        "database": health.check_database(settings.database_url),
        "bucket": health.check_bucket(
            settings.s3_bucket, settings.s3_region, settings.s3_endpoint_url
        ),
    }
    healthy = all(result == "ok" for result in checks.values())
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={"status": "ok" if healthy else "unhealthy", "checks": checks},
    )
