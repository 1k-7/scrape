# Dockerfile
# Use a modern, supported version of Python on Debian "Bookworm"
FROM python:3.11-bookworm

# Set the working directory in the container
WORKDIR /app

# Install system dependencies required for Selenium, Google Chrome, and build steps
RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    unzip \
    jq \
    # Install Chrome using the modern, secure method
    && wget -q -O - https://dl-ssl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /usr/share/keyrings/google-chrome-keyring.gpg \
    && sh -c 'echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome-keyring.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google.list' \
    && apt-get update \
    && apt-get install -y google-chrome-stable \
    # Clean up to reduce layer size before the next steps
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Install Chromedriver to match the stable Chrome version
# Using the new official Chrome for Testing (CfT) JSON endpoints
RUN LATEST_CHROME_VERSION=$(google-chrome --version | cut -d ' ' -f 3) \
    && echo "Detected Chrome version: $LATEST_CHROME_VERSION" \
    && CHROME_MAJOR_VERSION=$(echo $LATEST_CHROME_VERSION | cut -d '.' -f 1) \
    && echo "Major Chrome version: $CHROME_MAJOR_VERSION" \
    && CV_URL="https://googlechromelabs.github.io/chrome-for-testing/known-good-versions-with-downloads.json" \
    && DRIVER_URL=$(wget -qO- "$CV_URL" | jq -r ".versions[] | select(.version | startswith(\"$CHROME_MAJOR_VERSION\")) | .downloads.chromedriver[] | select(.platform==\"linux64\") | .url" | tail -n 1) \
    && echo "Chromedriver download URL: $DRIVER_URL" \
    && wget -q "$DRIVER_URL" -O /tmp/chromedriver.zip \
    && unzip /tmp/chromedriver.zip -d /tmp \
    # The zip file extracts to a directory like "chromedriver-linux64"
    && mv /tmp/chromedriver-linux64/chromedriver /usr/bin/chromedriver \
    && chmod +x /usr/bin/chromedriver \
    && rm -rf /tmp/chromedriver.zip /tmp/chromedriver-linux64

# Copy the requirements file into the container
COPY requirements.txt .
# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code into the container
COPY . .

# Command to run the application
CMD ["python", "main.py"]
