# One image for every target. Hugging Face Spaces, Lightsail, EC2 and Fargate
# all run a container and pass a port; nothing below is specific to any of them,
# so the platform stays a deploy-time choice rather than a code change.
FROM python:3.13-slim

# Non-root, and UID 1000 specifically: Hugging Face Spaces runs containers as
# that user and a root-owned working directory is not writable there.
RUN useradd --create-home --uid 1000 app
WORKDIR /home/app

# Requirements first, so a code change does not re-resolve every dependency.
COPY --chown=app:app requirements.txt pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=app:app src/ ./src/
COPY --chown=app:app gallery/ ./gallery/
RUN pip install --no-cache-dir --no-deps -e .

USER app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=7860 \
    MAS_ACCESS=gallery

# 7860 is what Hugging Face expects; every other platform reads $PORT anyway.
EXPOSE 7860

# A report takes 25-40s on a background thread with in-memory job state, so
# this must stay a single process. No --workers: a second worker would answer
# polls for jobs it has never heard of.
CMD ["sh", "-c", "python -m mas.web.cli --host 0.0.0.0 --port ${PORT}"]
