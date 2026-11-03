FROM public.ecr.aws/lambda/python:3.11

WORKDIR ${LAMBDA_TASK_ROOT}

COPY pyproject.toml ./
COPY src ./src

RUN pip install --no-cache-dir .

CMD ["texas_energy.lambda_handler.handler"]
