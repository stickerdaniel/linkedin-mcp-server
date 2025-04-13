# Generated by https://smithery.ai. See: https://smithery.ai/docs/config#dockerfile
FROM python:3.12-slim

# Install Chrome dependencies
RUN apt-get update && apt-get install -y \
    wget \
    unzip \
    libnss3 \
    libgconf-2-4 \
    libxi6 \
    libgdk-pixbuf2.0-0 \
    libxrandr2 \
    ca-certificates \
    fonts-liberation \
    libappindicator3-1 \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libgtk-3-0 \
 && rm -rf /var/lib/apt/lists/*

# Set work directory
WORKDIR /app

# Copy the project files
COPY . /app

# Upgrade pip and install build dependencies
RUN pip install --upgrade pip \
    && pip install --no-cache-dir .

# Expose any ports if necessary (MCP likely communicates via stdio so no port exposure)

# Set default command to run the MCP server
CMD ["python", "main.py", "--no-setup"]
