# AHMAG curator bot · сборка для Railway
# Python-бот + Node.js и Chrome для Remotion (вёрстка рилсов). Пакеты Chrome — список из документации Remotion
# для Debian: без них браузер для рендера не запустится.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 DEBIAN_FRONTEND=noninteractive PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg \
        libnss3 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libgbm1 libasound2 \
        libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libxkbcommon0 libpango-1.0-0 libcairo2 \
        libx11-xcb1 libxshmfence1 fonts-liberation \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

# Remotion: зависимости и браузер для рендера — отдельным слоем, чтобы не качать заново при каждом деплое
COPY remotion/package.json remotion/package-lock.json remotion/
RUN cd remotion && npm ci --no-audit --no-fund && npx remotion browser ensure

COPY . .
RUN cd remotion && npx remotion bundle src/index.ts --out-dir build --log=error

CMD ["python", "main.py"]
