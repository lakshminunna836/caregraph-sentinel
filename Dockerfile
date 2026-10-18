FROM python:3.12-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY mcp_server.py .
ENV PORT=8000
CMD ["sh", "-c", "uvicorn app.api:api --host 0.0.0.0 --port ${PORT}"]
