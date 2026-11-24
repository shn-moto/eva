FROM python:3.12-slim
# cups-client: lp / lpstat / cancel against the home's CUPS; DejaVu: Cyrillic in the PDFs made here.
# LibreOffice (headless): office files sent to Eva (odt, docx, xlsx, ...) become PDFs she reads and prints;
# Liberation / Carlito / Caladea: the metric twins of Times, Arial, Courier, Calibri, Cambria - Word keeps its layout
RUN apt-get update && apt-get install -y --no-install-recommends cups-client fonts-dejavu-core \
        libreoffice-writer-nogui libreoffice-calc-nogui libreoffice-impress-nogui libreoffice-draw-nogui \
        fonts-liberation fonts-crosextra-carlito fonts-crosextra-caladea \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir fastapi==0.162.0 "uvicorn[standard]==0.54.0" pillow==11.3.0 fpdf2==2.8.4 pypdf==6.1.1
WORKDIR /app
COPY print_server.py /app/
EXPOSE 8909
CMD ["uvicorn", "print_server:app", "--host", "0.0.0.0", "--port", "8909"]
