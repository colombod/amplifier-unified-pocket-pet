FROM espressif/idf:v5.5.4@sha256:b9f2d6ea1c19e0c9f7959bdb74a9e3c775642f9d0f3b841937c5fa3363db892b
# Explicit image preparation only. Restrict Pillow to known CPython 3.12
# Linux wheels for the actual SDK image architecture; never build from sdist.
RUN /usr/bin/python3 -c 'import platform, sys; assert sys.implementation.name == "cpython" and sys.version_info[:2] == (3, 12) and platform.machine() in ("x86_64", "aarch64"), "Unsupported SDK Python/architecture"' \
    && printf '%s\n' \
       'Pillow==11.3.0 \' \
       '    --hash=sha256:67172f2944ebba3d4a7b54f2e95c786a3a50c21b88456329314caaa28cda70f6 \' \
       '    --hash=sha256:676b2815362456b5b3216b4fd5bd89d362100dc6f4945154ff172e206a22c024 \' \
       '    --hash=sha256:eb76541cba2f958032d79d143b98a3a6b3ea87f0959bbe256c0b5e416599fd5d \' \
       '    --hash=sha256:97f07ed9f56a3b9b5f49d3661dc9607484e85c67e27f3e8be2c7d28ca032fec7' > /tmp/pet-assets-requirements.txt \
    && /usr/bin/python3 -m venv /opt/pet-assets \
    && /opt/pet-assets/bin/pip --isolated install --index-url https://pypi.org/simple \
       --no-cache-dir --only-binary=:all: --require-hashes -r /tmp/pet-assets-requirements.txt \
    && /opt/pet-assets/bin/python -c 'import PIL; assert PIL.__version__ == "11.3.0"' \
    && rm /tmp/pet-assets-requirements.txt
