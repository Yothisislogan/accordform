# WiTNext-only forms

The production `app.py` entrypoint is now a handoff page. It imports no database,
session, renderer, profile, email, AI, intake or Hedge module. Every write method
returns 410 without reading its body. No customer path or query string is copied
into the WiTNext link. Set `WITNEXT_ORIGIN` to the HTTPS WiTNext origin.

Deploy this only with the paired WiTNext forms workspace and coordinated data
migration. Do not use the historical Hedge installer on this release. Its
installer, probe and release-manifest files were retired along with their old
runtime-specific tests; the historical versions remain in Git at
`494e378ebc47389989cb2ce86693daf90d47b29b`. Historical application logic retained
under `tests/legacy_app.py` is a regression fixture, never a deployment entrypoint.

The new nginx configuration rejects writes before request-body buffering,
disables access logging and prevents proxy body forwarding. The systemd unit
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

Run `python -m pytest -q`. New tests exercise no-file/no-session handoff behavior,
all retired write paths, no answer echo, actual calendar validation, hidden-field
exclusion, flattened PDF output, mapping failure, and checksum-bound signatures.
Blank template approval and real carrier/signature acceptance require the
licensed documents and a controlled deployment pilot.
