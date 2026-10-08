# Chromium and its system libraries come with Playwright's own image (same version as requirements.txt).
FROM mcr.microsoft.com/playwright/python:v1.56.0-noble
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --break-system-packages -r requirements.txt
COPY main.py .
ENV PORT=8080
ENV PYTHONUNBUFFERED=1
# One worker process: the sessions live in its memory. Its threads only queue work for the one browser thread.
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "8", "--timeout", "120", "main:app"]
