FROM python:3.12-slim

# curl is needed by the docker-compose healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 5000

# IMPORTANT: exactly one worker (-w 1). Download state lives in-process (in-memory
# dict, ThreadPoolExecutor, and subprocess handles), so multiple workers would each
# hold separate state. Threads handle concurrent requests including SSE.
CMD ["gunicorn", "-w", "1", "--threads", "8", "-b", "0.0.0.0:5000", "app:app"]
