FROM python:3.13-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py .

RUN useradd --system --no-create-home app
USER app

EXPOSE 8000
CMD ["python", "server.py"]
