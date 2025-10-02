# Security Policy

## Supported Versions
Currently the project is early-stage and releases are not yet versioned. Treat `main` as latest.

## Reporting a Vulnerability
Please create a **private security advisory** or email the maintainer instead of opening a public issue if you believe you've found:
- Remote code execution
- Data exfiltration
- Authentication bypass (future feature)
- Denial of service vector beyond ordinary high request volume

If you cannot submit a private advisory, open an issue with minimal detail and request a secure contact channel.

## Best Practices for Deploying
- Run behind a reverse proxy (nginx/Traefik) if exposing to the internet.
- Add basic auth / OAuth proxy for public deployments.
- Restrict `/api/stream/samples` if traffic metadata is sensitive.
- Keep container image updated; rebuild periodically to obtain base image fixes.
- Use read-only filesystem or non-root user if you harden the Dockerfile (see Hardening section below).

## Hardening Suggestions
Add this to your Dockerfile (validate app still runs):
```
RUN adduser --disabled-password --gecos '' appuser \
  && chown -R appuser:appuser /app
USER appuser
```
Mount database directory with least privileges.

## Data Stored
Only latency samples and outage intervals (no PII). Timestamps + success flags + latency ms.

## Cryptography / Secrets
No secrets stored. If you add webhooks or alerting later, keep tokens outside the repo via environment variables.

## Rate Limiting / Abuse
Not implemented. Consider adding a simple reverse proxy rate limit if exposing publicly.
