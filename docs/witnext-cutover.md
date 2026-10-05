# Keep Forms at its own address; retain customer data only in WiTNext

The production `app.py` entrypoint serves the WiT Forms portal at the existing
Forms address. It embeds the paired WiTNext `/forms-app/` surface, which has
customer selection, saved packages and the complete editor without the CRM
shell. It never redirects to a customer workspace or displays a moved notice.

The Forms host imports no database, session, renderer, profile, email, AI, intake
or Hedge module. Every write method returns 410 without reading its body. No
incoming path, query string, cookie or credential is copied into the frame. The
only parent/frame messages are versioned readiness flags. Customer answers,
documents and tokens never enter the Forms parent page. All customer-bearing
requests connect directly from the WiTNext frame to WiTNext APIs and storage.

## Paired deployment and connection

1. Reconcile the paired WiTNext branch with the active release, deploy migration
   0067 and the new API/web images, and complete the licensed-template review.
2. Set `WITNEXT_ORIGIN` to the actual HTTPS WiTNext origin in
   `/etc/wit-forms/witforms.env`. This is public configuration, not a secret.
   Never assume a staging or production default. The app does not load `.env`.
3. Deploy this portal with the supplied nginx config and read-only service after
   adapting their filesystem paths. Keep the existing Forms hostname.
4. WiTNext web nginx permits `https://forms.weinsurethings.com` as an ancestor
   only on `/forms-app/`. Update that exact origin if the Forms hostname differs.
   Ensure the edge does not add a conflicting frame policy to this route. Keep
   the CRM and API protected and retain normal Access authentication.
5. Open Forms. If sign-in is needed, use **Connect to WiTNext** in a new tab,
   then return and **Reload forms**. The portal waits for an authenticated Forms
   readiness reply; an iframe load alone is not treated as success. Check the
   deployed browser's cookie policy with sibling HTTPS origins.
6. Verify fictional create/edit/save/reopen, PDF review/print/download, signature
   and Hedge controls from Forms. Browser network traffic containing customer
   data must target WiTNext, never the Forms host. Check an unauthorized user
   and a stale second tab. `/healthz` must report `forms-portal`, `configured:
   true`, and `customer_storage: false`. Verify both `/forms-app/` and a deep
   link have the narrow frame policy and `Cache-Control: no-store`.

If connection fails, fix WiTNext authentication/configuration. Do not restore
local SQLite writes, proxy cookies or send credentials in URLs as a fallback.

Deploy this only with the paired WiTNext Forms surface and coordinated data
transition. Do not use the historical Hedge installer on this release. Its
installer, probe and release-manifest files were retired along with their old
runtime-specific tests; the historical versions remain in Git at
`494e378ebc47389989cb2ce86693daf90d47b29b`. Historical application logic retained
under `tests/legacy_app.py` is a regression fixture, never a deployment entrypoint.

The new nginx configuration rejects writes before request-body buffering,
disables access/error logging and prevents proxy body forwarding. The service
discards stdout/stderr, disables core dumps, and exposes only the non-sensitive
health endpoint for monitoring. This prevents malformed request diagnostics from
retaining customer-supplied paths or headers. The systemd unit
uses `/opt/wit-forms-new`, a read-only filesystem and no customer write directory.
Review actual live paths and edge configuration before installing examples.

`renderer.py` runs **inside WiTNext**, with licensed blank templates mounted
read-only. It accepts stdin, returns a PDF in memory, rejects unresolved or
ambiguous targets, checks template/schema/mapping hashes and requires a reviewed
omission decision for every unrepresented PDF field. The schema files are not
evidence that every ACORD field is correct or current.

Before retiring old storage, inventory SQLite, PDFs, Hedge/intake records,
credentials, temporary files, logs and backups. Transfer customer records to
reviewed WiTNext accounts; verify counts, hashes, access and restoration. Only
then remove the old retained copies under the approved retention procedure.
This commit neither migrates nor deletes existing customer data. A read-only
service does not erase old data or backups.

Run `python -m pytest -q` and `node --test tests/portal.test.mjs`.
New tests exercise no-file/no-session portal behavior and trusted readiness,
all retired write paths, no answer echo, actual calendar validation, hidden-field
exclusion, flattened PDF output, mapping failure, and checksum-bound signatures.
Blank template approval and real carrier/signature acceptance require the
licensed documents and a controlled deployment pilot.
