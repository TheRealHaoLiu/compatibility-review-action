FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY compat_review ./compat_review
RUN pip install --no-cache-dir ".[service]"
ENV COMPAT_REVIEW_DATA=/data PORT=8000
VOLUME ["/data"]
EXPOSE 8000
CMD ["compatibility-review-service"]
