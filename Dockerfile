# Use AWS Lambda Python runtime
FROM public.ecr.aws/lambda/python:3.11

# Install system dependencies if needed (for PyMuPDF compilation)
# RUN yum install -y gcc-c++ && yum clean all

# Copy requirements and install dependencies
COPY requirements.txt ${LAMBDA_TASK_ROOT}

# Install Python dependencies
# Note: boto3 is already available in Lambda runtime, but keeping it for compatibility
RUN pip install --no-cache-dir -r requirements.txt

# Copy function code
COPY index.py ${LAMBDA_TASK_ROOT}

# Set the CMD to your handler
CMD ["index.lambda_handler"]