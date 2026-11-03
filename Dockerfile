FROM python:3.13-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 FILEAPI_ROOT=/data FILEAPI_STATE_DIR=/state PORT=8000
COPY pyproject.toml README.md ./
COPY fileapi ./fileapi
RUN pip install --no-cache-dir . && useradd -r -u 10001 app && mkdir -p /data /state && chown app /data /state
USER app
VOLUME /data /state
EXPOSE 8000
CMD ["fileapi"]
