FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 


RUN apt-get update && apt-get install -y \
    build-essential \
    python3-dev \
    libpq-dev \
    gcc \
    curl \
    gettext \
    libgdbm-dev \
    chromium \
    chromium-driver \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*


WORKDIR /app

ENV CHROME_BIN=/usr/bin/chromium
ENV CHROMEDRIVER_PATH=/usr/bin/chromedriver

COPY ../../backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ../../backend/ .

# CMD ["python", "infinity"]
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]