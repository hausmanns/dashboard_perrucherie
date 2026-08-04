# Dashboard de la Perrucherie — all-in-one image.
# The SQLite DB is NOT baked into the image: it is bind-mounted from ./data
# (see docker-compose.yml) so it stays a normal file in the repo checkout
# and keeps travelling with git.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first for layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static

# Data directory (DB created on first start; usually bind-mounted from host).
RUN mkdir -p /app/data

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
