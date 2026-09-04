FROM python:3.11-slim

# WeasyPrint needs Pango/Cairo/GDK-Pixbuf for HTML->PDF rendering, plus
# Liberation fonts so the PDF renders consistent, Arial-metric-compatible
# text regardless of the host machine.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libcairo2 \
    libgdk-pixbuf-2.0-0 \
    libffi-dev \
    shared-mime-info \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /srv/data \
    && chown appuser:appuser /srv/data
USER appuser

EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
