FROM python:3.12-slim

WORKDIR /app
COPY server/requirements.txt /app/server/requirements.txt
RUN pip install --no-cache-dir -r /app/server/requirements.txt

# the server imports electridrive.licensing
COPY electridrive /app/electridrive
COPY server /app/server

WORKDIR /app/server
ENV PORT=8000
EXPOSE 8000
CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:8000", "license_server:create_app()"]
