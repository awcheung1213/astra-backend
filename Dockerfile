FROM python:3.12-slim

# send python logs to the console
ENV PYTHONUNBUFFERED 1

# add /app to the PYTHONPATH
ENV PYTHONPATH /app

# set /app path as the default directory inside the container
WORKDIR /app

# Install PostgreSQL client tools for pg_isready
RUN apt-get update && \
   apt-get install -y postgresql-client && \
   apt-get clean && \
   rm -rf /var/lib/apt/lists/*

# install all dev requirements
COPY ./requirements-dev.txt /app/requirements-dev.txt
RUN pip install -r requirements-dev.txt

# install all requirements
COPY ./requirements.txt /app/requirements.txt
RUN pip install -r requirements.txt

# copy all files into the container
COPY . /app

# expose port 8000
EXPOSE 8000

# One image, two Railway services — only the start command differs:
#   api:    (this CMD) uvicorn on $PORT; migrations run via preDeployCommand (railway.json)
#   worker: python -m worker.run                             (railway.worker.json)
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}
