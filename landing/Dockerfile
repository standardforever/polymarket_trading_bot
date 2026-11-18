FROM node:22-alpine
WORKDIR /app
COPY package.json server.mjs config.js index.html ./
COPY css ./css
COPY js ./js
COPY img ./img
COPY data ./data
ENV LANDING_HOST=0.0.0.0 LANDING_PORT=5180
EXPOSE 5180
CMD ["npm", "start"]
