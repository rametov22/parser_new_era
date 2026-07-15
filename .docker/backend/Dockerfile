FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 


# Refresh the browser package layer on rebuilds. Chromium 150.0.7871.46
# crashes on startup in Debian containers; the security update fixes it.
RUN apt-get -o Acquire::Retries=3 update \
    && apt-get install --no-install-recommends -y \
    build-essential \
    python3-dev \
    libpq-dev \
    gcc \
    curl \
    gettext \
    libgdbm-dev \
    chromium \
    chromium-driver \
    xvfb \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*


WORKDIR /app

ENV CHROME_BIN=/usr/bin/chromium
ENV CHROMEDRIVER_PATH=/usr/bin/chromedriver

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ .

# CMD ["python", "infinity"]
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
