# worker/Dockerfile
FROM python:3.11-slim

# Otimizações básicas
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Dependências de sistema para PyMuPDF (fitz), numpy etc.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libfreetype6-dev \
    libjpeg62-turbo-dev \
    libopenjp2-7 \
    liblcms2-2 \
    libwebp7 \
    libharfbuzz0b \
    libfribidi0 \
    libxcb1 \
    && rm -rf /var/lib/apt/lists/*


WORKDIR /app

# Requisitos Python
COPY worker/requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

# Código do worker
COPY worker/ /app/

# Variáveis que você injeta na Fly (secrets)
# OPENAI_API_KEY, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, BUCKET_DOCS, BUCKET_RESULTS etc.
# (não coloque valores aqui; use fly secrets)

# Comando — ajuste se seu worker tem CLI diferente
CMD ["python", "-u", "worker.py"]
