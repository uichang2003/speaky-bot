FROM python:3.11-slim

WORKDIR /app

# ✅ 음성 재생(FFmpeg) + PyNaCl 빌드/런타임에 필요한 것들
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libffi-dev \
    libsodium-dev \
    && rm -rf /var/lib/apt/lists/*

# ✅ 의존성 먼저 설치(캐시 효율)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ✅ 소스 복사
COPY . .

# ✅ 봇 실행 (파일명이 스피키.py인 현재 구조에 맞춤)
CMD ["python", "스피키.py"]
