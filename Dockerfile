FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    fonts-dejavu-core \
    curl \
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
