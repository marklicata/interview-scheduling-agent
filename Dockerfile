FROM python:3.12-slim

WORKDIR /app

# Install the agent with M365 host dependencies only.
# copilot and dev extras are excluded — copilot requires a local GitHub login
# and is not suitable for a headless container host.
COPY agent/ ./agent/
RUN pip install --no-cache-dir "./agent[m365]"

# Run as a non-root user.
RUN useradd --create-home --no-log-init appuser
USER appuser

# Azure App Service and Container Apps route traffic to PORT.
# HOST must be 0.0.0.0 so the server accepts connections from outside the container.
ENV PORT=8000
ENV HOST=0.0.0.0

EXPOSE 8000

CMD ["python", "-m", "scheduling_agent.hosts.m365.app"]
