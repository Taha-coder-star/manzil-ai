# Pinned by digest (docs/release/RELEASE.md: refreshing the pins).
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# requirements.lock is generated from requirements.txt with hashes (RELEASE.md).
COPY requirements.lock .
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

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

# The commit this image was built from: docker build --build-arg GIT_SHA=$(git rev-parse HEAD)
ARG GIT_SHA=unknown
LABEL org.opencontainers.image.revision=$GIT_SHA
RUN printf '%s\n' "$GIT_SHA" > /app/REVISION

RUN useradd --create-home appuser
USER appuser

EXPOSE 8000

ENTRYPOINT ["python", "-m", "app"]
CMD ["web"]
