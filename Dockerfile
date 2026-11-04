FROM python:3.13-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 FILEAPI_ROOT=/data FILEAPI_STATE_DIR=/state FILEAPI_GIT_ROOT=/repos PORT=8000
COPY pyproject.toml README.md ./
COPY fileapi ./fileapi
RUN pip install --no-cache-dir . && useradd -r -u 10001 app && mkdir -p /data /state /repos && chown app /data /state /repos
USER app
VOLUME /data /state /repos
EXPOSE 8000
CMD ["fileapi"]
