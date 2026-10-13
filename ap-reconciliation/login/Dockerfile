# The AP reconciliation sign-in page: one static page served by nginx on Cloud Run,
# public, in front of the IAP-guarded dashboard. See ../README.md, "Access and roles".
FROM nginx:1.27-alpine

RUN rm /etc/nginx/conf.d/default.conf
COPY nginx.conf /etc/nginx/templates/default.conf.template

# The page is a template until the container starts: 40-dashboard-url.sh fills in
# DASHBOARD_URL (the nginx entrypoint runs every *.sh in /docker-entrypoint.d).
COPY index.html /usr/share/nginx/html/index.template.html
COPY 40-dashboard-url.sh /docker-entrypoint.d/40-dashboard-url.sh
RUN chmod +x /docker-entrypoint.d/40-dashboard-url.sh

ENV PORT=8080
EXPOSE 8080
CMD ["nginx", "-g", "daemon off;"]
