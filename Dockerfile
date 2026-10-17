FROM node:20-bookworm-slim AS dashboard-build
WORKDIR /build
COPY package.json package-lock.json ./
COPY apps/dashboard/package.json ./apps/dashboard/package.json
RUN npm ci --ignore-scripts
COPY apps/dashboard ./apps/dashboard
RUN npm run build --workspace apps/dashboard

FROM python:3.11-slim
WORKDIR /app
COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt
COPY backend ./backend
COPY .env.example ./
COPY --from=dashboard-build /build/apps/dashboard/dist ./apps/dashboard/dist
EXPOSE 8000
CMD ["uvicorn","backend.app.main:app","--host","0.0.0.0","--port","8000"]
