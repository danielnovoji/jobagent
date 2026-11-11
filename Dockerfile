FROM python:3.12-slim

# LibreOffice Writer for DOCX→PDF, poppler-utils for pdfinfo/pdftotext,
# Carlito (metric-compatible Calibri substitute — required to keep CV on one page),
# Hebrew-capable fonts for cover notes.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libreoffice-writer \
        poppler-utils \
        fonts-crosextra-carlito \
        fonts-dejavu-core \
        fonts-noto \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p data out secrets

CMD ["python", "run.py"]
