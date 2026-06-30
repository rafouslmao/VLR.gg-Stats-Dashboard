FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Extract pre-scraped data if data.zip is present and data/ doesn't exist
RUN if [ -f data.zip ] && [ ! -d data ]; then \
        apt-get update -qq && apt-get install -y --no-install-recommends unzip && \
        unzip -q data.zip && \
        apt-get purge -y unzip && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*; \
    fi

EXPOSE 8080

CMD ["python", "app.py"]
