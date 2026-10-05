from fastapi import FastAPI

app = FastAPI(title="Manzil AI")


@app.get("/live")
def live():
    return {"status": "running", "role": "web"}