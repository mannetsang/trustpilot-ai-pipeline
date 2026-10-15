FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py ./
COPY static/ ./static/
ENV PORT=8080
ENV PYTHONUNBUFFERED=1
# One worker keeps the sheet cache and the IAP key cache in one place; threads serve the page.
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "8", "--timeout", "120", "main:app"]
