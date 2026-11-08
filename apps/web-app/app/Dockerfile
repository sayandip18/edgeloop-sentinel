FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY app.py .
USER 10001:10001
CMD ["python", "app.py"]
