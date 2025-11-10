FROM python:3.11-slim

# install tesseract inside the container
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr tesseract-ocr-eng libtesseract-dev libleptonica-dev \
  && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

RUN mkdir -p /app/uploads
EXPOSE 5000
CMD ["python", "app.py"]
