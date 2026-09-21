# Perseus fork

Cookie-session auth for Jira Data Center behind an SSO gate (Cognito ALB).

Upstream: https://github.com/sooperset/mcp-atlassian

Jira's own REST accepts `JSESSIONID` as session auth
([cookie-based authentication](https://developer.atlassian.com/server/jira/platform/cookie-based-authentication/)).
Upstream already has `ATLASSIAN_EXTERNAL_AUTH_ENABLE`. This fork also
treats a `Cookie` header or `JIRA_COOKIE` as that mode, so a PAT is not
required.

```bash
JIRA_URL=https://jira.example.com
JIRA_COOKIE='JSESSIONID=…; AWSELBAuthSessionCookie-0=…'
```

`JIRA_COOKIE` is not comma-split. A PAT still wins if `JIRA_PERSONAL_TOKEN` is set.

Streamable HTTP no longer 401s when a cookie session (or
`ATLASSIAN_EXTERNAL_AUTH_ENABLE`) is configured: that *is* the user
identity, so `ALLOW_GLOBAL_CRED_FALLBACK` is not required.
