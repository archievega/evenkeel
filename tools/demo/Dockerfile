# VHS plus the two tools the tape drives. Pinned rather than `latest`, so
# re-recording a year from now produces the same GIF from the same tape.
FROM ghcr.io/charmbracelet/vhs:v0.10.0

# The upstream image tracks the `stable` suite, and `stable` has since rolled
# from bookworm to trixie — apt refuses to update across a codename change, and
# pulling trixie packages onto a bookworm base would be the worse fix. Pinning
# the codename this image was actually built on keeps it installable.
RUN sed -i 's/Suites: stable/Suites: bookworm/; s/stable-updates/bookworm-updates/; s/stable-security/bookworm-security/' \
        /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install --no-install-recommends -y curl jq \
    && rm -rf /var/lib/apt/lists/*
