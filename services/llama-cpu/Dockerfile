# llama.cpp server для shn-linux: Xeon E5-2650 v2 (Ivy Bridge) — AVX есть,
# AVX2/FMA/F16C нет, поэтому собираем из исходников с явными флагами.
FROM debian:bookworm-slim AS build
RUN apt-get update && apt-get install -y --no-install-recommends \
      git cmake build-essential ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch v0.9.0 https://github.com/ggml-org/llama.cpp /src
WORKDIR /src
RUN cmake -B build \
      -DBUILD_SHARED_LIBS=OFF -DGGML_BACKEND_DL=OFF \
      -DGGML_NATIVE=OFF -DGGML_AVX=ON -DGGML_AVX2=OFF \
      -DGGML_FMA=OFF -DGGML_F16C=OFF -DGGML_BMI2=OFF -DGGML_RPC=OFF \
      -DLLAMA_CURL=OFF -DLLAMA_BUILD_EXAMPLES=OFF -DLLAMA_BUILD_TESTS=OFF \
    && cmake --build build --config Release -j 14 --target llama-server

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /src/build/bin/llama-server /usr/local/bin/llama-server
EXPOSE 8902
ENTRYPOINT ["llama-server"]
