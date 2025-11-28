# Use an official Python runtime as a parent image
FROM python:3.9-slim

# Install git (Required for cloning repos)
RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*

# Set the working directory
WORKDIR /app

# Copy the current directory contents into the container
COPY . /app

# Install any needed packages
RUN pip install fastapi uvicorn google-generativeai pydantic

# Make port 8000 available to the world outside this container
EXPOSE 8000

# Define environment variable (You would set the key in the cloud dashboard)
ENV GEMINI_API_KEY=""

# Run the app
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]