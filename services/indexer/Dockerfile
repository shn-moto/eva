FROM python:3.12-slim
RUN pip install --no-cache-dir fastembed "psycopg[binary]"
WORKDIR /app
COPY indexer.py /app/indexer.py
CMD ["python", "indexer.py"]
