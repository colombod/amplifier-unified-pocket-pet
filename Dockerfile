FROM espressif/idf:v5.5.4@sha256:f3bc9daebfd8cb4e0afbdced7caac4431c7a862326a5e5587d65c31439549bb5
RUN python3 -m venv /opt/pet-assets && /opt/pet-assets/bin/pip install --no-cache-dir Pillow==11.3.0
