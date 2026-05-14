# Static dashboard served by nginx on Cloud Run.
# Listens on $PORT (Cloud Run sets this to 8080 by default).
FROM nginx:alpine

# Strip default nginx config, replace with ours
RUN rm /etc/nginx/conf.d/default.conf
COPY nginx.conf /etc/nginx/templates/default.conf.template

# Copy the dashboard
COPY index.html /usr/share/nginx/html/index.html

# Cloud Run injects $PORT. envsubst (built into nginx:alpine) expands it at runtime.
ENV PORT=8080
EXPOSE 8080

# nginx:alpine entrypoint already runs envsubst on templates -> conf.d
CMD ["nginx", "-g", "daemon off;"]
