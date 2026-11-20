FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first, so code changes don't reinstall them
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

COPY . .

# The commit this image was built from (set by deploy.sh); recorded in every session log
ARG GIT_COMMIT=unknown
ENV GIT_COMMIT=$GIT_COMMIT

RUN useradd --create-home trios \
    && mkdir -p /data/logs \
    && chown trios /data/logs
USER trios
