# Lambda container image for the nightly job search.
# Built on the official Python image plus awslambdaric (the Lambda runtime interface
# client), so the DejaVu font that the PDF report uses can be installed with apt.
# Build for arm64 with provenance off (Lambda rejects multi-manifest images):
#   docker build --platform linux/arm64 --provenance=false -t job-search .
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.7.13 /uv /bin/uv

# Unicode font for the PDF, and time zone data so date.today() (report names, the
# open/closed check) is Pacific time rather than the Lambda's UTC.
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core tzdata \
    && rm -rf /var/lib/apt/lists/*

ENV LAMBDA_TASK_ROOT=/var/task \
    HOME=/tmp \
    TZ=America/Los_Angeles \
    PYTHONPATH=/var/task/src \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR ${LAMBDA_TASK_ROOT}

# Install exactly the locked runtime dependencies (no dev tools) plus the Lambda extra.
COPY pyproject.toml uv.lock ./
RUN uv export --frozen --no-dev --extra lambda --no-emit-project -o requirements.txt \
    && uv pip install --system --no-cache -r requirements.txt \
    && rm requirements.txt

COPY config.yaml ./
COPY src ./src

ENTRYPOINT ["python", "-m", "awslambdaric"]
CMD ["job_search.lambda_handler.handler"]
