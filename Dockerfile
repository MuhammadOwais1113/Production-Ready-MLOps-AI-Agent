# Inference service image (deployed by kubernetes/deployment.yaml).
# Build:  python scripts/prepare_model_dir.py && docker build -t mlops-inference:1.0.0 .
FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN pip install fastapi==0.115.* "uvicorn[standard]==0.32.*" scikit-learn joblib numpy pydantic
COPY serving/ /app/serving/
COPY models/ /models/
RUN useradd -u 10001 appuser
USER 10001
EXPOSE 8000
CMD ["uvicorn", "serving.app:app", "--host", "0.0.0.0", "--port", "8000"]
