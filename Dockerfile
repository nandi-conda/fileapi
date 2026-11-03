FROM python:3.13-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 FILEAPI_ROOT=/data PORT=8000
COPY pyproject.toml README.md ./
COPY fileapi ./fileapi
RUN pip install --no-cache-dir . && useradd -r -u 10001 app && mkdir -p /data && chown app /data
USER app
VOLUME /data
EXPOSE 8000
CMD ["fileapi"]
