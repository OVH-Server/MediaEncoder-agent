FROM python:3.12-slim

# ffmpeg statique BtbN (build GPL, inclut h264_nvenc / hevc_nvenc).
# Les bibliothèques NVIDIA (libnvidia-encode…) sont injectées à l'exécution
# par le nvidia-container-toolkit (NVIDIA_DRIVER_CAPABILITIES=video).
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl xz-utils ca-certificates && \
    curl -fsSL https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz -o /tmp/ffmpeg.tar.xz && \
    tar -xf /tmp/ffmpeg.tar.xz -C /tmp && \
    mv /tmp/ffmpeg-master-latest-linux64-gpl/bin/ffmpeg /tmp/ffmpeg-master-latest-linux64-gpl/bin/ffprobe /usr/local/bin/ && \
    rm -rf /tmp/ffmpeg* && \
    apt-get purge -y curl xz-utils && apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY src/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ .

CMD ["python3", "/app/app.py"]
