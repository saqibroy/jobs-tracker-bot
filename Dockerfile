FROM python:3.11-slim

WORKDIR /app

# Install system dependencies for Playwright (for future scrapers)
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Create data and logs directories
RUN mkdir -p data logs

CMD ["python", "main.py"]
