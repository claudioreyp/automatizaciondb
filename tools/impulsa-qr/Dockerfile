FROM node:22-bookworm-slim AS build
WORKDIR /app
COPY package.json ./
COPY build-tooling/package.json build-tooling/package-lock.json /tooling/
RUN npm ci --prefix /tooling --ignore-scripts && ln -s /tooling/node_modules /app/node_modules
COPY qr-direct-runtime/package.json qr-direct-runtime/package-lock.json ./qr-direct-runtime/
RUN cd qr-direct-runtime && npm ci --omit=dev --ignore-scripts
COPY tsconfig.json tsconfig.qr.json ./
COPY src ./src
COPY scripts/build.mjs ./scripts/build.mjs
RUN node scripts/build.mjs

FROM node:22-bookworm-slim AS runtime
ENV NODE_ENV=production IMPULSA_QR_LISTEN_HOST=0.0.0.0 IMPULSA_QR_SESSION_DIR=/var/lib/impulsa/session
WORKDIR /app
COPY --from=build --chown=node:node /app/dist ./dist
COPY --from=build --chown=node:node /app/qr-direct-runtime ./qr-direct-runtime
COPY --chown=node:node package.json ./
COPY --chown=node:node scripts/start-qr.sh ./scripts/start-qr.sh
RUN chmod 0555 ./scripts/start-qr.sh && mkdir -p /var/lib/impulsa/session && chown node:node /var/lib/impulsa/session
USER node
STOPSIGNAL SIGTERM
ENTRYPOINT ["/bin/sh", "/app/scripts/start-qr.sh"]
