FROM public.ecr.aws/lambda/python:3.12.2026.10.03.13-x86_64

# Into the function directory, which Lambda searches first: the pinned boto3, botocore and urllib3
# take precedence over the base image's copies.
COPY requirements-lambda.txt ./
RUN pip install --no-cache-dir --target "${LAMBDA_TASK_ROOT}" -r requirements-lambda.txt && rm requirements-lambda.txt

COPY lambda_function.py ${LAMBDA_TASK_ROOT}/
COPY extraction/ ${LAMBDA_TASK_ROOT}/extraction/

CMD ["lambda_function.lambda_handler"]
