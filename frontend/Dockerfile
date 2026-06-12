ARG NODE_BASE_IMAGE=node:22-alpine
FROM ${NODE_BASE_IMAGE}

WORKDIR /app/frontend

COPY frontend/package.json ./
RUN npm install

COPY frontend ./

EXPOSE 5173

CMD ["npm", "run", "dev"]
