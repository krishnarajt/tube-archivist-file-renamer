FROM python:3.12-slim
WORKDIR /app
COPY rename_from_metadata.py ./
ENTRYPOINT ["python", "/app/rename_from_metadata.py"]
