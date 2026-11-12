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
# Public benchmark gold only. Private audio/manifests/reviewed gold are mounted
# separately; the optional Day13 budget validates its frozen public inputs.
COPY bench/customer_text.json bench/customer_voice.json bench/voice_controls.json \
     bench/staff.json bench/staff_spans.json bench/scope_conversations.json \
     bench/day13_thresholds.json bench/day13_gold_decisions.json ./bench/

RUN useradd --create-home appuser
USER appuser

EXPOSE 8000

ENTRYPOINT ["python", "-m", "app"]
CMD ["web"]
