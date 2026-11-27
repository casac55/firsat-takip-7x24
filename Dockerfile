FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Europe/Istanbul \
    DASHBOARD_HOST=0.0.0.0

RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata build-essential libxml2-dev libxslt1-dev zlib1g-dev libjpeg-dev curl \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir -r requirements.txt

COPY . /app

EXPOSE 8050
CMD ["python", "main.py", "--daemon"]
