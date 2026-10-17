# Образ сервиса платформы. Собирается из корня репозитория (context: .. в infra/compose.yaml):
#   ядро core/platform_core + модули leads, dealer_cabinet, billing + миграции infra/db.
# Ключей в образе нет: они приходят только из окружения сервера (ARCHITECTURE.md, 7.4).
FROM python:3.12-slim
# Привязка пакета GHCR к репозиторию (docs.github.com, «Working with the Container registry»).
LABEL org.opencontainers.image.source=https://github.com/Vitaalii/dealer-platform

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY core/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY core/platform_core /app/platform_core
COPY leads/leads /app/leads
COPY dealer-cabinet/dealer_cabinet /app/dealer_cabinet
COPY billing/billing /app/billing
COPY infra/db /app/db

ARG PLATFORM_VERSION=dev
ENV PLATFORM_VERSION=${PLATFORM_VERSION}

RUN useradd --system --uid 10001 app
USER app
EXPOSE 8000
CMD ["python", "-m", "platform_core"]
