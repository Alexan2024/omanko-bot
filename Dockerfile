FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py .
COPY Nunito-SemiBold.ttf .

# Проверяем что шрифт скопирован
RUN ls -la Nunito-SemiBold.ttf && echo "Шрифт OK"

CMD ["python", "bot.py"]
