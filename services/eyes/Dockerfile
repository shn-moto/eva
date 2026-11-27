# Eva's eyes (eyes.py): motion and faces on the CPU - OpenCV + YuNet, no GPU (it is full).
# Who a face is: photolib's /search/face, its own face base.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir fastapi==0.162.0 "uvicorn[standard]==0.54.0" "psycopg[binary]==3.2.10" \
    numpy opencv-python-headless==4.12.0.88
# YuNet face detector (OpenCV zoo, 230 KB, MIT)
RUN mkdir -p /models && curl -fsSL -o /models/face_detection_yunet_2023mar.onnx \
    https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx \
    && test $(stat -c %s /models/face_detection_yunet_2023mar.onnx) -gt 100000
WORKDIR /app
COPY eyes.py /app/
EXPOSE 8913
CMD ["uvicorn", "eyes:app", "--host", "0.0.0.0", "--port", "8913"]
