# One image for both HR ATS Cloud Run jobs:
#   indeed-pipeline  -> default CMD (new Indeed applications -> Supabase)
#   offer-packet     -> deployed with --command python --args offer_packet.py,--run
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY ats_db.py indeed_pipeline.py offer_packet.py ./
CMD ["python", "indeed_pipeline.py", "--run"]
