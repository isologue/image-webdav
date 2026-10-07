FROM python:3.12-alpine
WORKDIR /app
RUN pip install --no-cache-dir fastapi==0.115.12 uvicorn==0.34.2 passlib==1.7.4 python-multipart==0.0.20
COPY app.py cleanup.py index.html /app/
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
