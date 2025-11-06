FROM python:3.11-slim

# libs do PyMuPDF
RUN apt-get update && apt-get install -y --no-install-recommends \
    libmujs0 libx11-6 libxext6 libxrender1 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY worker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY worker/anchors_reading_span.py ./anchors_reading_span.py
COPY worker/main.py ./main.py

ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host=0.0.0.0", "--port=8000"]
