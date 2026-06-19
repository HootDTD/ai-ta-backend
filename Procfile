web: uvicorn server:app --host 0.0.0.0 --port ${PORT:-8000}
worker: python -m teacher_upload_worker
apollo-janitor: python -m apollo.learner_janitor_worker
