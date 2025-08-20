# Используем официальный образ Python 3.11
FROM python:3.11-slim

# Устанавливаем рабочую директорию внутри контейнера
WORKDIR /app

# Копируем файл с зависимостями
COPY requirements.txt .

# Устанавливаем зависимости из requirements.txt
# Шаг с 'build-essential' убран для ускорения сборки
RUN pip install --no-cache-dir -r requirements.txt

# Копируем все остальные файлы проекта в контейнер
COPY . .

# Команда для запуска бота будет взята из вашего Procfile,
# поэтому CMD здесь не нужен.
