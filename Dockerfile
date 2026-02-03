# Use official Python runtime as base image
FROM python:3.11-slim

# Set working directory in container
WORKDIR /app

# Install system dependencies (if needed)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first (for better caching)
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY demo.py .

# Create non-root user for security
RUN useradd -m -u 1000 appuser && \
    chown -R appuser:appuser /app
USER appuser

# Expose port
EXPOSE 5000

# Health check
# Increased start-period to allow gunicorn workers to fully initialize
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:5000/ || exit 1

# Run with gunicorn for production
# Timeout set to 120s to accommodate slow Jenkins responses (Jenkins timeout is 60s by default)
# Preload app to ensure faster startup and better error handling
# Graceful timeout for worker restarts
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--timeout", "120", "--graceful-timeout", "30", "--preload", "--access-logfile", "-", "--error-logfile", "-", "demo:app"]