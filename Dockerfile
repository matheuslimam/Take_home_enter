# Dockerfile
FROM python:3.11-slim

# Depêndencias básicas (certificados, locale, etc.)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates locales tzdata curl \
 && rm -rf /var/lib/apt/lists/*

# Locale (opcional)
RUN sed -i 's/# pt_BR.UTF-8 UTF-8/pt_BR.UTF-8 UTF-8/' /etc/locale.gen && locale-gen
ENV LANG=pt_BR.UTF-8
ENV LC_ALL=pt_BR.UTF-8
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PORT=8080

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Código
COPY app.py ./app.py
COPY worker ./worker

# Se você lê .env localmente, copie .env.example -> e use Secrets no Fly para prod
# COPY .env ./.env

EXPOSE 8080
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
