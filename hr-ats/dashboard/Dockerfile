# Build the Vite app, then run the lightweight production server.
FROM node:24-slim AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM node:24-slim
WORKDIR /app
ENV NODE_ENV=production
COPY --from=build /app/dist ./dist
COPY server.mjs mail.mjs ./
EXPOSE 8080
CMD ["node", "server.mjs"]
