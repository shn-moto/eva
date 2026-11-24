FROM python:3.12-slim
# cups-client: lp / lpstat / cancel against the home's CUPS; DejaVu: Cyrillic in the PDFs made here.
# No LibreOffice here: office files are converted by the host's own LibreOffice (convert_host.py).
RUN apt-get update && apt-get install -y --no-install-recommends cups-client fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir fastapi==0.162.0 "uvicorn[standard]==0.54.0" pillow==11.3.0 fpdf2==2.8.4 pypdf==6.1.1
WORKDIR /app
COPY print_server.py /app/
EXPOSE 8909
CMD ["uvicorn", "print_server:app", "--host", "0.0.0.0", "--port", "8909"]
