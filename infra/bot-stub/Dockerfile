# Заглушка бота до этапа 7: только стандартная библиотека Python.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY stub.py /app/stub.py
RUN useradd --system --uid 10001 app
USER app
EXPOSE 8080
CMD ["python", "/app/stub.py"]
