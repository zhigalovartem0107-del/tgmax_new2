# Used by Bothost when "Использовать собственный Dockerfile" is on:
# a plain Python 3.12 image (no Node.js) that runs main.py.
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# /app/data (SQLite database) is the folder Bothost keeps between deploys.
CMD ["python", "main.py"]
