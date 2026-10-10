FROM python:3.13-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install --no-install-recommends -y tesseract-ocr tesseract-ocr-spa \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN useradd --create-home --uid 10001 agent \
    && mkdir -p /app/data \
    && chown -R agent:agent /app

USER agent

CMD ["uvicorn", "agent.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]