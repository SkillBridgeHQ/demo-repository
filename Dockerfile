FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -r requirements.txt

RUN groupadd --system skillbridge \
    && useradd --system --gid skillbridge --create-home skillbridge

COPY --chown=skillbridge:skillbridge alembic.ini .
COPY --chown=skillbridge:skillbridge alembic/ ./alembic/
COPY --chown=skillbridge:skillbridge app/ ./app/

USER skillbridge

EXPOSE 8000

CMD ["sh", "-c", "python -m alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]