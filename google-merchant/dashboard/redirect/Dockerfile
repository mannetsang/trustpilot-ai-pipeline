FROM python:3.12-slim
WORKDIR /app
COPY server.py .
ENV PORT=8080 PYTHONUNBUFFERED=1
CMD ["python", "server.py"]
