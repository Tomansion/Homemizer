FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Run as a normal user, and own /data before the volume is attached: Docker
# copies the image's ownership onto a fresh named volume, which is how the
# SQLite cache stays writable without running everything as root.
RUN useradd --system --uid 10001 appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /data /app
USER appuser

ENV CACHE_DB=/data/cache.db

EXPOSE 8000

# --proxy-headers/--forwarded-allow-ips make the reverse proxy's X-Forwarded-For
# the client address. Without them every request looks like it came from the
# proxy and the per-IP rate limits would throttle the whole site as one user.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
