FROM python:3.12-slim

WORKDIR /srv

COPY requirements.txt /srv/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py /srv/server.py
COPY secrets.json /srv/secrets.json

EXPOSE 8080
CMD ["python", "/srv/server.py"]
