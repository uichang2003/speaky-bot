FROM python:3.12-slim

ARG CACHE_BUST=2

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg curl unzip ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# ✅ deno 설치 (yt-dlp가 유튜브 JS 처리에 사용)
RUN curl -fsSL https://deno.land/install.sh | sh
ENV DENO_INSTALL="/root/.deno"
ENV PATH="$DENO_INSTALL/bin:$PATH"

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "main.py"]
