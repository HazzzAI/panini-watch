# Panini Watch - always-on container (Playwright image already contains Chromium + system libraries)
FROM mcr.microsoft.com/playwright/python:v1.55.0-noble
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY panini_watch ./panini_watch
COPY config.yaml .
ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "panini_watch", "run"]
