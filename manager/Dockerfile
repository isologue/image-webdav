FROM python:3.12-alpine
WORKDIR /app
RUN pip install --no-cache-dir fastapi==0.115.12 uvicorn==0.34.2 passlib==1.7.4 python-multipart==0.0.20 WsgiDAV==4.3.3 itsdangerous==2.2.0
COPY app.py auth.py start.py cleanup.py index.html login.html /app/
EXPOSE 8000
CMD ["python", "start.py"]
