FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    fonts-dejavu-core \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY Nunito-VariableFont_wght.ttf .
RUN ls -la /app/Nunito-VariableFont_wght.ttf && echo "Шрифт скачан OK" || echo "ERROR: Шрифт не скачался"
# Скачиваем шрифт напрямую при сборке образа
#RUN curl -L "https://github.com/google/fonts/raw/main/ofl/nunito/static/Nunito-SemiBold.ttf" \
#    -o /app/Nunito-SemiBold.ttf && \
#    ls -la /app/Nunito-SemiBold.ttf && \
#    echo "Шрифт скачан OK"

COPY bot.py .

CMD ["python", "bot.py"]
