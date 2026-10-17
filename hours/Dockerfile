FROM node:25-alpine
WORKDIR /srv
COPY package.json .
RUN npm install --omit=dev --no-audit --no-fund
COPY server.js .
EXPOSE 3000
USER node
CMD ["node", "server.js"]
