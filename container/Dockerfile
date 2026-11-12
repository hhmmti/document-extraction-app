# The run viewer (app/main.py) for the container platform.  The build context is the app folder container/build.py
# writes: this file beside app/, extraction/ and .streamlit/config.toml.
FROM public.ecr.aws/docker/library/python:3.12-slim
WORKDIR /app
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
# /app holds only what the app runs on.  It starts from /app on app/main.py itself, so that .streamlit/config.toml
# applies and Streamlit serves app/static/ (the fonts).
COPY .streamlit/config.toml .streamlit/config.toml
COPY app app
COPY extraction extraction
EXPOSE 80
CMD ["streamlit", "run", "--server.port", "80", \
     "--server.address", "0.0.0.0", \
     "--server.enableCORS", "false", \
     "--server.enableXsrfProtection", "false", \
     "--server.headless", "true", \
     "--server.fileWatcherType", "none", \
     "--server.baseUrlPath", "/enterprise/esp-design-extraction", \
     "app/main.py"]
