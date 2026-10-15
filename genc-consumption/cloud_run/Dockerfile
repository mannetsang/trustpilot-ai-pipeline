FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py ./
COPY static/ ./static/
ENV PORT=8080
ENV PYTHONUNBUFFERED=1
# One worker keeps the sheet cache, the IAP key cache and the voice sessions in one place;
# threads serve the page, and each open voice connection holds one.
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "16", "--timeout", "120", "main:app"]
