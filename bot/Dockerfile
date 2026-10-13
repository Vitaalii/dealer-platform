# 1. Базовый образ с Python 3.10
FROM python:3.10-slim

# 2. Устанавливаем рабочую директорию внутри контейнера
WORKDIR /app

# 3. Копируем файл зависимостей
COPY requirements.txt .

# 4. Устанавливаем библиотеки
RUN pip install --no-cache-dir -r requirements.txt

# 5. Копируем все файлы проекта (main.py, database.py и др.)
COPY . .

# Открываем порт для веб-сервера
EXPOSE 8080

# 6. Команда для запуска бота
CMD ["python", "main.py"]