FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY reconcile_reviews.py .
ENTRYPOINT ["python", "reconcile_reviews.py"]
CMD ["--since-hours", "48"]
