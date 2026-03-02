FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
COPY rename_from_metadata.py ./
ENTRYPOINT ["python", "/app/rename_from_metadata.py"]