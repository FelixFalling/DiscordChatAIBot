# Use a modern Python base image
FROM python:3.12-slim

# Specify your e-mail address as the maintainer of the container image
LABEL maintainer="nphua@pdx.edu"

# Set the working directory
WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code
COPY . .

# Environment variables
ENV PYTHONUNBUFFERED=1

# Run the Discord bot
CMD ["python", "-u", "app.py"]
