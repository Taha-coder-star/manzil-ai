FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY migrations ./migrations
# Fictional demo dataset (python -m app.seed) and the demo media plan / intake
# manifests (python -m app.media_seed); data only, no images.
COPY demo ./demo

RUN useradd --create-home appuser
USER appuser

EXPOSE 8000

ENTRYPOINT ["python", "-m", "app"]
CMD ["web"]