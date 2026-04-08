FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .
COPY config.example.yaml .
COPY cc_proxy/ cc_proxy/

# 如果没有 config.yaml 则从示例创建
RUN if [ ! -f config.yaml ]; then cp config.example.yaml config.yaml; fi

EXPOSE 5566

CMD ["python", "main.py"]
