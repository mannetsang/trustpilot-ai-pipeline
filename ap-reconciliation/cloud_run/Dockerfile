FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py ./
COPY static/ ./static/
ENV PORT=8080
ENV PYTHONUNBUFFERED=1
# One worker: the index/match/notify jobs serialize on an in-process lock, and
# the ID-token cache lives in the process. Eight threads keep the dashboard
# responsive while a job runs. 30 minutes matches the Cloud Run request
# timeout, because a synchronous upload (?sync=1) reads every new invoice
# before it returns.
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "8", "--timeout", "1800", "main:app"]
