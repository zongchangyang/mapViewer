FROM python:3.11-slim

WORKDIR /app

# Runtime libs needed by rasterio's bundled GDAL.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libexpat1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

# GDAL tuning for cloud-optimized COG reads over HTTP range requests.
# See https://developmentseed.org/titiler/advanced/performance_tuning/
ENV PORT=8080 \
    GDAL_HTTP_MULTIPLEX=YES \
    GDAL_HTTP_VERSION=2 \
    GDAL_DISABLE_READDIR_ON_OPEN=EMPTY_DIR \
    GDAL_INGESTED_BYTES_AT_OPEN=32768 \
    CPL_VSIL_CURL_ALLOWED_EXTENSIONS=.tif,.TIF,.tiff \
    CPL_MACHINE_IS_GCE=YES \
    VSI_CACHE=TRUE \
    VSI_CACHE_SIZE=536870912

EXPOSE 8080

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
