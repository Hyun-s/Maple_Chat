# Inven Rights and Live-Crawl Gate

## Default state

`LIVE_CRAWL_ENABLED=false` is mandatory by default. Development and automated tests use only
sanitized fixtures under `tests/fixtures/inven`. The application does not treat `robots.txt`
or technical accessibility as permission to copy, embed, or redistribute content.

## Approval evidence

Evidence is stored outside Git (the ignored `compliance/evidence/` directory is the suggested
location) as JSON with this minimum structure:

```json
{
  "status": "approved",
  "evidence_type": "written_permission",
  "approved_at": "2026-08-04T00:00:00Z",
  "approved_by": "reviewer-or-authority",
  "scopes": ["approved_sample"]
}
```

`evidence_type` is `written_permission`, `legal_review`, or an explicit project-owner
`operator_authorization`. Supported scopes are:

- `approved_sample`: explicitly approved low-volume sample access
- `full_backfill`: complete historical collection; never implied by sample approval

Opening the gate requires both `LIVE_CRAWL_ENABLED=true` and a valid
`LIVE_CRAWL_APPROVAL_FILE`. Code validates every target as HTTPS, exact allowlisted Inven host,
and approved scope before network I/O.

## Mandatory stop conditions

Stop the affected board immediately and do not bypass controls when any of these occur:

- repeated `403` or `429`, including a `Retry-After` window
- CAPTCHA, login, or private-content requirement
- parser/schema drift or required selector loss
- request to rotate IPs, evade controls, or perform an Inven write action
- raw PII or secret detected in persistence, logs, fixtures, or output
- missing, invalid, expired, or scope-mismatched approval evidence

Recovery requires a documented review, corrected fixture/parser or approval record, and a
bounded revalidation. A successful code build never opens the public-release gate by itself.

