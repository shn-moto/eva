FROM python:3.12-slim
RUN pip install --no-cache-dir fastapi==0.162.0 "uvicorn[standard]==0.54.0" "psycopg[binary]==3.2.10"
WORKDIR /app
COPY admin.py index.html /app/
EXPOSE 8908
CMD ["uvicorn", "admin:app", "--host", "0.0.0.0", "--port", "8908"]
