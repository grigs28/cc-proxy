FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .
COPY .env.example .env.example
COPY cc_proxy/ cc_proxy/

# 如果没有 .env 则从示例创建
RUN if [ ! -f .env ]; then cp .env.example .env; fi

EXPOSE 5566 5567

CMD ["python", "main.py"]
