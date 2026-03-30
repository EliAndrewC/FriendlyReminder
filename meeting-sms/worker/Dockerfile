FROM python:3.12-slim

WORKDIR /app

# System deps for audio processing
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg libsndfile1 && rm -rf /var/lib/apt/lists/*

# Install PyTorch CPU-only (much smaller than CUDA version)
RUN pip install --no-cache-dir torch torchaudio --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Reinstall torchcodec CPU-only (the default pulls CUDA deps)
RUN pip uninstall -y torchcodec || true
RUN pip install --no-cache-dir torchcodec-cpu || pip install --no-cache-dir torchcodec --index-url https://download.pytorch.org/whl/cpu || true

COPY worker.py .
COPY generate_greeting.py .
COPY ref_audio/ ref_audio/

CMD ["python", "worker.py"]
# cache-bust: 1774866077
