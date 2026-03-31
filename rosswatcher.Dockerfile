FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir curl_cffi
COPY main.py .
EXPOSE 8081
CMD ["python3", "-u", "main.py"]
