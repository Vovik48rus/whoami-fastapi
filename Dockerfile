FROM python:3.12-slim

WORKDIR /srv/app

# Ускоряем и стабилизируем сборку
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Не root-пользователь. Явно чиним владельца и права на скопированные файлы:
# COPY наследует права из build-контекста, и если исходники на хосте были
# созданы с ограниченными правами (например, 600), appuser не сможет их
# прочитать — отсюда PermissionError при импорте модуля uvicorn'ом.
RUN useradd --create-home --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /srv/app \
    && chmod -R u+rX,go+rX /srv/app
USER appuser

EXPOSE 8000

# Приложение слушает только HTTP: uvicorn запускается без
# --ssl-keyfile/--ssl-certfile — TLS/SSL-терминация выполняется на
# Apache перед этим контейнером.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
