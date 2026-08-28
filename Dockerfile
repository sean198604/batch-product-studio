FROM python:3.11-slim

# Non-buffered logs + domestic PyPI mirror for faster builds in China.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn

WORKDIR /app

# Install dependencies first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source.
COPY . .

# Ensure mount points exist (persisted via volumes at runtime).
RUN mkdir -p storage/uploads storage/outputs data

# Single worker is REQUIRED: the queue + task state are process-local.
EXPOSE 7021
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "7021", "--workers", "1"]
