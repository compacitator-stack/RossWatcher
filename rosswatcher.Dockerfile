FROM python:3.11-slim
WORKDIR /app
COPY main.py .
EXPOSE 8081
CMD ["python3", "-u", "main.py"]
