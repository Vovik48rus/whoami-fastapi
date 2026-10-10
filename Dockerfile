FROM python:3.12-slim

WORKDIR /srv/app

# Ускоряем и стабилизируем сборку. UV_PYTHON_DOWNLOADS=never: uv использует
# интерпретатор из образа и никогда не скачивает другой.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1

# Версия uv зафиксирована; она совпадает с той, что создала uv.lock.
RUN pip install uv==0.12.24

# Зависимости ставятся отдельным слоем строго по uv.lock (--locked падает,
# если uv.lock не соответствует pyproject.toml). Dev-зависимости (pytest) не нужны.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --locked --no-dev

COPY app ./app

# Не root-пользователь. Явно чиним владельца и права на скопированные файлы:
# COPY наследует права из build-контекста, и если исходники на хосте были
# созданы с ограниченными правами (например, 600), appuser не сможет их
# прочитать — отсюда PermissionError при импорте модуля uvicorn'ом.
RUN useradd --create-home --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /srv/app \
    && chmod -R u+rX,go+rX /srv/app
USER appuser

# Окружение проекта (.venv) первым в PATH: uvicorn берётся оттуда.
ENV PATH="/srv/app/.venv/bin:$PATH"

EXPOSE 8000

# Приложение слушает только HTTP: uvicorn запускается без
# --ssl-keyfile/--ssl-certfile — TLS/SSL-терминация выполняется на
# Apache перед этим контейнером.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

