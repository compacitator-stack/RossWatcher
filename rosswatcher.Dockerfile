FROM python:3.11-slim
WORKDIR /app
COPY rosswatcher.py .
EXPOSE 8081
CMD ["python3", "-u", "rosswatcher.py"]
