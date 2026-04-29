# BESS dispatch (Pyomo + HiGHS). Build context should include specification.txt, prices.csv, and src/.
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    BESS_SPEC=/app/specification.txt

WORKDIR /app

STOPSIGNAL SIGTERM

# highspy / Pyomo wheels may need a compiler on some platforms
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY docker/requirements.txt requirements.txt
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . .

# Default: run optimisation. Override spec path: docker run -e BESS_SPEC=/app/other.txt …
CMD ["sh", "-c", "exec python3 src/bess_dispatch_opt.py --spec \"$BESS_SPEC\""]
