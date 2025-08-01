# Используем стандартный образ Python
FROM python:3.11-slim

# Устанавливаем системные зависимости, необходимые для компиляции
# пакетов вроде xgboost и scikit-learn.
RUN apt-get update && apt-get install -y --no-install-recommends build-essential

# Устанавливаем рабочую директорию внутри контейнера
WORKDIR /app

# Копируем файл с зависимостями и устанавливаем их
# Этот шаг теперь выполнится успешно, так как компиляторы установлены
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем остальной код вашего приложения
COPY . .

# Указываем команду для запуска вашего бота
# Убедитесь, что ваш главный файл называется main.py
CMD ["python", "main.py"]
