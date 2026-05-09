FROM node:20-alpine
WORKDIR /app
ENV NODE_ENV=production \
    PORT=3000
COPY package.json ./
RUN npm install --omit=dev --no-audit --no-fund
COPY server.js ./
EXPOSE 3000
CMD ["node", "server.js"]
