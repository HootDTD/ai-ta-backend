web: uvicorn server:app --host 0.0.0.0 --port ${PORT:-8000} --workers ${WEB_CONCURRENCY:-4}
worker: python -m teacher_upload_worker
