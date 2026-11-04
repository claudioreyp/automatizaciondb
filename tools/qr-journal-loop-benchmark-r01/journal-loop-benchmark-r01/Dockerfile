ARG BASE_IMAGE
FROM ${BASE_IMAGE}
USER root
COPY . /opt/escalar-journal-bench/
RUN node /opt/escalar-journal-bench/apply-patch.cjs
USER node
