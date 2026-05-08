FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml ./
COPY agent ./agent
COPY api ./api
COPY approval ./approval
COPY audit ./audit
COPY booking ./booking
COPY cli ./cli
COPY memory ./memory
COPY reviews ./reviews
COPY tools ./tools

RUN pip install --upgrade pip && pip install -e .

EXPOSE 8080

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8080"]
