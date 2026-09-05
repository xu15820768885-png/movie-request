FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    TZ=Asia/Shanghai

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY workflow.py .
COPY dian115_openapi.py .
COPY hdhive_openapi.py .
COPY guanying_client.py .
COPY web ./web

# Fail the image build if an application module was omitted from the image.
RUN python -c "import app, workflow"

EXPOSE 5056
CMD ["python", "/app/app.py"]
