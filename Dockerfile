FROM --platform=$BUILDPLATFORM python:3.12-slim AS builder
WORKDIR /build
COPY pyproject.toml .
RUN python -c "import tomllib;d=tomllib.load(open('pyproject.toml','rb'))['project']['dependencies'];open('req.txt','w').write('\n'.join(x for x in d if not x.startswith('boto3'))+'\nboto3>=1.44\n')" \
 && pip install --no-cache-dir -r req.txt \
    --platform manylinux2014_aarch64 --platform manylinux_2_28_aarch64 \
    --only-binary=:all: --python-version 3.12 --implementation cp --target /deps

FROM --platform=linux/arm64 public.ecr.aws/lambda/python:3.12
COPY --from=builder /deps ${LAMBDA_TASK_ROOT}
COPY photo_sync/ ${LAMBDA_TASK_ROOT}/photo_sync
CMD ["photo_sync.indexer.handler"]
