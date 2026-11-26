# What a sound is (docs/EMOTION-PLAN.md): PANNs CNN14 on the CPU. The checkpoint is not in
# the image: the cache volume (~/eva-data/bench-cache -> /root/.cache) holds panns_data.
FROM python:3.11-slim
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu
RUN pip install --no-cache-dir panns_inference fastapi==0.162.0 "uvicorn[standard]==0.54.0" numpy
# PANNs looks for its checkpoint and labels in ~/panns_data
RUN ln -s /root/.cache/panns_data /root/panns_data
WORKDIR /app
COPY events_server.py /app/
EXPOSE 8911
CMD ["uvicorn", "events_server:app", "--host", "0.0.0.0", "--port", "8911"]
