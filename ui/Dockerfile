FROM node:22-alpine

WORKDIR /app

COPY package.json ./
COPY config.js index.html app.js styles.css server.mjs ./

ENV UI_HOST=0.0.0.0 \
    UI_PORT=5173

EXPOSE 5173

CMD ["npm", "start"]
