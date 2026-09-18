# Hedge + We Insure Things intake

Code is prepared for deployment; no live brokerage credentials or production
submission have been used. Deploy the matching changes in `Yothisislogan/catdog`.

## Data flow

1. Shopper completes `/submit-a-risk/` on WordPress and grants consent.
2. WordPress validates the data and signs a server-to-server request to WIT Forms.
3. WIT Forms atomically saves a local draft and returns a reference. The shopper
   sees “received for review”; nothing has been sent to Hedge at this point.
4. Staff sign into `forms.weinsurethings.com/hedge`, select the Website request,
   inspect its payload, and select **Send to Hedge**. This starts Hedge intake
   and may start instant carrier API quoting. It does not finalize marketing.
5. Staff attach PDFs, check requirements, review, explicitly approve, and finalize.
6. Verified webhook market events appear in submission activity. Refresh status
   or the existing background poller supplies overall submission state.

Personal home/auto shoppers keep the existing contact route. Supporting PDFs
are uploaded by staff through the existing workflow; this release does not
accept anonymous file uploads or expose a consumer quote/status portal.

## Server configuration

Use a separate staging instance and separate `DATA_DIR` / `DB_PATH`. Do not
switch a database containing staging submissions to production credentials.
Back up the SQLite database before deploying. Startup creates the new tables
without rewriting existing rows. Retain the database and its replay records
across restarts and deployments. Run under the existing restricted service user.

Put these values in `/etc/wit-forms/witforms.env` (mode 0600):

```dotenv
HEDGE_ENV=staging
HEDGE_CLIENT_ID=
HEDGE_CLIENT_SECRET=
HEDGE_SCOPES=broker_mcp broker_submit
HEDGE_LIVE=0
HEDGE_WEBHOOK_SECRET=
WIT_INTAKE_SECRET=
```

Obtain the client ID (`bac_…`) and secret (`bas_…`) from Hedge portal **Settings
→ API keys**. A brokerage admin must create a key with submission scope, and
Hedge must enable programmatic submission access for the brokerage. The staff
email attributed as producer must be an active portal user of that brokerage.
Remove obsolete `HEDGE_API_KEY` / `HEDGE_API_KEY_HEADER` settings.

The server discovers the token endpoint and uses OAuth client credentials;
temporary tokens stay in process memory. The existing encrypted device-login
flow remains available when no machine credential is configured.

Generate a separate random secret for `WIT_INTAKE_SECRET`, for example
`python -c 'import secrets; print(secrets.token_hex(32))'`, and transfer it
privately to WordPress. Do not reuse the Hedge secret or webhook signing key.

Install `requirements.txt`, restart the service, and confirm `/healthz`. The
optional SendGrid dependency is pinned to the available `6.12.5` release;
the previous `6.12.6` pin could not be installed from the package index.

## WordPress

The companion theme's `docs/HEDGE-INTAKE.md` contains the full setup. Set these
server-only constants in `wp-config.php` before the “stop editing” line:

```php
define('WIT_FORMS_INTAKE_URL', 'https://forms.weinsurethings.com/integrations/wit/intake');
define('WIT_INTAKE_SECRET', getenv('WIT_INTAKE_SECRET'));
```

The environment value must match WIT Forms. Use your staging forms host while
testing. No Hedge credential belongs in WordPress, JavaScript, or a repository.
The WordPress server needs outbound HTTPS to the forms host.

Create the `submit-a-risk` page with the **Submit a Business Risk** template via
**Tools → WIT Page Setup**. Exclude this page and the REST endpoint from full-page
and CDN caches. Use HTTPS. Publish after staging verification and review of the
displayed contact/consent/privacy text. The commercial and specialty hub buttons
appear only when the bridge is configured and the page is published.

## Hedge webhooks

In the matching Hedge portal environment, a brokerage admin registers:

`https://forms.weinsurethings.com/integrations/hedge/events`

Save the endpoint's `whsec_…` secret as `HEDGE_WEBHOOK_SECRET` and restart.
Send the portal's signed ping and check for a 200 response. Registration is a
portal action; the machine credential cannot manage webhook destinations.
Keep the URL free of redirects and accessible through the reverse proxy.

Verification uses the exact raw body, `svix-id`, `svix-timestamp`, and
`svix-signature`, with a five-minute timestamp tolerance. Acknowledgement follows
the SQLite commit and makes no outbound HTTP call. Duplicate delivery IDs are
acknowledged; the event identity also includes type so a later quote arrival
is retained. Market-level events never imply the entire submission is bound.

Webhooks deliver new events only. Use Hedge's portal delivery log to inspect
failures and redeliver after fixing them. Existing submissions still support
status polling; this release does not import historical market events.

## Bridge contract

`POST /integrations/wit/intake`, JSON body (32 KiB maximum).

- `X-WIT-Timestamp`: Unix seconds; accepted within five minutes.
- `X-WIT-Signature`: lowercase hex HMAC-SHA256 using `WIT_INTAKE_SECRET` over
  `wit-risk-v1.` + timestamp + `.` + the exact JSON bytes.
- Body: UUID `request_id`, `business_name`, `first_name`, `last_name`, `email`,
  `phone`, `address`, `city`, `state`, `zip`, `operations`, `lines`, boolean
  `consent=true`, and `consent_version=wit-risk-v1`. Optional: `effective_date`
  (ISO date), `revenue`, `employees`, `current_insurance`, `losses`.
- Accepted: 201 `{reference, status: "received_for_review"}`; identical retry:
  200. A reference reused with changed content: 409. Invalid signature: 401;
  invalid input: 422; disabled configuration: 503.

No public read endpoint exists. Consumer data cannot set a producer email,
existing insured ID, remote status, or approval. Consent version and receipt time
are retained with the local intake. Do not add PII or credentials to URLs/logs.

## Acceptance and recovery

- With live writes off, submit a synthetic shopper request and confirm exactly
  one Website draft and one receipt. Retry the identical request.
- Confirm staff authentication is required to read the queue and send a draft.
- Verify machine identity in `/hedge`; enable `HEDGE_LIVE=1` on staging only.
- Send the test draft, upload a test PDF, check requirements, review and finalize.
- Verify a real signed webhook and a redelivery; confirm only one event appears.
- Repeat the credential, webhook and smoke checks on production before launch.

A timeout from website intake must be retried with the same request reference.
The form retains only a digest/reference in session storage, not answers. If
closing the page after an uncertain outcome, preserve the reference and ask an
agent to check before starting another request.

Hedge create retries retain the same idempotency key and frozen producer. After
23 hours an uncertain create is blocked before Hedge's 24-hour replay window
expires: reconcile with the Hedge portal first. Do not delete the draft to retry.
Turning `HEDGE_LIVE=0` pauses outbound writes while retaining website intake and
webhook receipt; disable the WordPress bridge to pause public intake as well.

Automated checks: `python -m pytest tests/ -q`. All upstream HTTP in these tests
is mocked. The licensed ACORD rendering acceptance test needs its template and
`pdftk`; it skips when they are absent.

## API references

- [Authentication](https://docs.hedgespecialty.com/authentication/)
- [Submission creation](https://docs.hedgespecialty.com/api/operations/createsubmission/)
- [Signed webhooks](https://docs.hedgespecialty.com/api/webhooks/receivesubmissionevents/)
- [Finalize](https://docs.hedgespecialty.com/api/operations/finalizesubmission/)
