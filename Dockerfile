FROM python:3.11-slim AS base

ARG INSTALL_PLAYWRIGHT=true

WORKDIR /app

# System dependencies for Playwright (only installed if INSTALL_PLAYWRIGHT=true)
RUN if [ "$INSTALL_PLAYWRIGHT" = "true" ]; then \
    apt-get update && apt-get install -y \
    gcc \
    wget \
    gnupg \
    ca-certificates \
    fonts-liberation \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libgbm1 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libx11-xcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libxss1 \
    libxtst6 \
    xdg-utils \
    && rm -rf /var/lib/apt/lists/*; \
    fi

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright Chromium only when enabled
RUN if [ "$INSTALL_PLAYWRIGHT" = "true" ]; then \
    playwright install chromium && \
    playwright install-deps chromium; \
    fi

COPY . .

# Create data and logs directories
RUN mkdir -p data logs

CMD ["python", "main.py"]
