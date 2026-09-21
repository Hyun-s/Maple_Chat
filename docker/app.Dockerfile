FROM python:3.12.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN groupadd --gid 10001 maplechat \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin maplechat

WORKDIR /app

COPY pyproject.toml README.md ./
COPY requirements/lock.txt ./requirements/lock.txt
COPY alembic.ini ./alembic.ini
COPY migrations ./migrations
COPY src ./src

RUN python -m pip install --no-cache-dir --constraint requirements/lock.txt .

USER 10001:10001

ENTRYPOINT ["maple-chat"]
