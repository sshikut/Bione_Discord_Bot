FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Koyeb 기본 포트 8000 노출
EXPOSE 8000

CMD ["python", "main.py"]