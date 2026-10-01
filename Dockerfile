FROM python:3.11-slim

# libfribidi0 — кернинг в заголовках (Pillow включает «умную» вёрстку текста)
# libcairo2   — SVG-логотипы партнёров
RUN apt-get update && apt-get install -y \
    fonts-dejavu-core \
    curl \
    libfribidi0 \
    libcairo2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY Nunito-SemiBold.ttf .
COPY NunitoSans-Black.ttf .
COPY Nunito-VariableFont_wght.ttf .
COPY *.png ./
COPY bot.py .

CMD ["python", "bot.py"]
