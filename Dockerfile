FROM alpine:latest

# Install Python and networking tools
RUN apk add --no-cache python3 iproute2

# Copy router code into container
COPY router.py /app/router.py

# Set working directory
WORKDIR /app

# Run the router
CMD ["python3", "-u", "router.py"]