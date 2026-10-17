# PostgreSQL платформы. Свой образ — чтобы в задаче 5.3 добавить сюда pgBackRest.
FROM postgres:17

# Скрипт первого запуска: базы и роли. Выполняется один раз, когда том данных пустой.
COPY init/ /docker-entrypoint-initdb.d/
