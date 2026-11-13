# JuneDesk processing worker: Docling (layout + tables), LibreOffice, ffmpeg, 7-Zip, readpst, Tesseract (highlight positions).
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/models \
    OMP_NUM_THREADS=2

RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg p7zip-full unar pst-utils \
      tesseract-ocr tesseract-ocr-eng tesseract-ocr-hin tesseract-ocr-mar tesseract-ocr-ben tesseract-ocr-guj tesseract-ocr-pan \
      tesseract-ocr-tam tesseract-ocr-tel tesseract-ocr-kan tesseract-ocr-mal tesseract-ocr-urd tesseract-ocr-ori \
      libreoffice-writer-nogui libreoffice-impress-nogui libreoffice-calc-nogui \
      fonts-noto-core fonts-noto-extra fonts-indic \
      libgl1 libglib2.0-0 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --extra-index-url https://download.pytorch.org/whl/cpu -r requirements.txt
# Fetch Docling's layout and table models at build time so the first file doesn't wait for downloads.
RUN docling-tools models download layout tableformer || echo "model prefetch skipped; models download on first use"

COPY convert.py render.py worker.py fresh.py ./
RUN useradd --create-home worker && mkdir -p /models && chown -R worker /app /models
USER worker

CMD ["python", "-u", "worker.py"]
