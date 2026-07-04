FROM python:3.11-slim

WORKDIR /srv

# requirements first: this layer only rebuilds when dependencies change
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/
COPY worker/ worker/
COPY dashboard/ dashboard/

EXPOSE 8000
# default command = API; the worker service overrides this in docker-compose
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
