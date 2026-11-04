FROM eclipse-temurin:21-jre
RUN apt-get update && apt-get install -y python3 python3-pip python3-venv && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN python3 -m venv /venv && /venv/bin/pip install --no-cache-dir -r requirements.txt
COPY app.py .
COPY chunker-cli.jar .
ENV PATH="/venv/bin:$PATH"
CMD uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}
