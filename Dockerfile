FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libxml2-dev \
    libxslt-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV HOMEHUNT_DB=/data/homehunt.db
ENV HOMEHUNT_CONFIG=london-search.yaml

EXPOSE 8000

CMD ["uvicorn", "homehunt.api:app", "--host", "0.0.0.0", "--port", "8000"]
