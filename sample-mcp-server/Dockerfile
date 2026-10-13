FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY data.py server.py ./
ENV PORT=8000
EXPOSE 8000
CMD ["python", "server.py"]
