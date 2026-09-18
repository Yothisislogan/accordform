# Deploy with a theme upload and forms-server terminal

This release is prepared for review and staging. Installing code does not supply
Hedge credentials, register a webhook, publish a page, or send a risk to Hedge.
The two draft pull requests are [forms #5](https://github.com/Yothisislogan/accordform/pull/5)
and [theme #13](https://github.com/Yothisislogan/catdog/pull/13).

## 1. Confirm the forms installation

In the forms server terminal, inspect the existing service:

```sh
systemctl show witforms --property=WorkingDirectory --property=ExecStart
```

The repository uses `/opt/wit-forms`, a `.venv/bin/gunicorn` command, service
`witforms`, and local port 8097. Confirm those against the output. Confirm the
active `DB_PATH` privately from the deployment configuration; its default is
`DATA_DIR/witforms.db`, with `DATA_DIR` defaulting to `/opt/wit-forms/data` in
this layout. Do not paste the environment file or credentials into chat.

Copy and extract `WIT-Hedge-Deployment.zip` on the server **outside** the existing
application. From its `WIT-Hedge-Deployment/forms-source` directory, run:

```sh
python3 tools/install_hedge_release.py --app-dir /opt/wit-forms
```

The server confirmed in this deployment uses `/opt/wit-forms-new` and is at
commit `af54e41`. That release and `5cbb24b` are both supported by exact file
hashes. The older release also receives the Hedge modules, mapping files,
ACORD 125 schema, and browser assets that were added after it. Existing
licensed PDFs and customer records are preserved.

For that server, check the code and report the database path in one read-only step:

```sh
python3 tools/install_hedge_release.py --app-dir /opt/wit-forms-new --inspect-service
```

Service inspection requires permission to read the running process environment
(the root terminal already has it). It displays only the database path and
whether the file exists, using the path rules from the verified configuration.
Use this reported path for `--db-path` when applying; also change `--app-dir`
to `/opt/wit-forms-new` in the installation command below.

This only checks the release and prints the changed files. It rejects conflicting
local application edits and reports every conflicting path together. Existing
`.env.example`, README, documentation, and test files are left untouched because
they are not required to run the integration. If it reports a conflict or your service uses another layout, retain
the output and adapt the deployment before applying. Do not overwrite a conflict.
The installer requires Python 3.9+ and the existing virtual environment; installing
dependencies also requires package-index network access and sufficient disk space.

## 2. Install on staging, then validate before production

Use a separate staging forms service/database and the matching staging Hedge
credentials. Pass its app directory, service name, database path, and health URL
to the installer. The defaults below describe the repository's usual production
layout; use them only after verifying the active installation.

After the check succeeds, apply using the **confirmed active database path**:

```sh
sudo python3 tools/install_hedge_release.py \
  --app-dir /opt/wit-forms \
  --db-path /opt/wit-forms/data/witforms.db \
  --apply
```

The installer prepares and checks a separate virtual environment before stopping
the service. It backs up changed code and a consistent SQLite snapshot, applies
the reviewed files, and starts the service. It verifies `/healthz` and the intake
route. An unsuccessful start/check restores the previous code and environment;
it retains database additions and the snapshot. Backups are under
`/var/backups/wit-forms/hedge-*`. It preserves the existing environment file,
licensed ACORD templates, uploaded documents, and other runtime files.

Only the manifest-listed application/dependency files are applied; files already
at the release version are skipped. The installer and this
guide are deployment tools; they need not be copied into the running application.
The manifest pins the integration payload to the reviewed forms commit. Keep the
bundle and backup until the site and live connection have been verified.

## 3. Configure forms and Hedge

Use `sudoedit /etc/wit-forms/witforms.env` to configure the values described in
`docs/HEDGE-SETUP.md`; preserve unrelated existing settings. Begin with
`HEDGE_ENV=staging` and `HEDGE_LIVE=0` on the staging instance. Hedge machine
credentials require both `HEDGE_CLIENT_ID` and `HEDGE_CLIENT_SECRET`, with
`HEDGE_SCOPES=broker_mcp broker_submit` and submission access enabled by Hedge.

Generate a separate bridge secret privately on the server:

```sh
python3 -c 'import secrets; print(secrets.token_hex(32))'
```

Set it as `WIT_INTAKE_SECRET` on the forms server and use the same value in the
WordPress settings in step 4. Hedge credentials and this secret are different.
Restart the service after environment changes: `sudo systemctl restart witforms`.

In the matching Hedge portal, register the public HTTPS endpoint:

`https://forms.weinsurethings.com/integrations/hedge/events`

Use the staging host for staging. Save its signing secret as
`HEDGE_WEBHOOK_SECRET`, restart the service, and verify a signed portal ping.
Follow `docs/HEDGE-SETUP.md` for scopes, event handling, and staging acceptance.

## 4. Upload and configure the WordPress theme

Back up the current theme and WordPress database. Confirm the active theme folder
matches `catdog`, the root folder in `WiT-Hedge-Theme.zip`. If the active folder
has a different name (for example `catdog-main`), repack the ZIP with that exact
folder name before replacing it. This avoids switching theme identity and
separating existing theme settings. Preserve any live theme edits absent from GitHub.

Upload **WiT-Hedge-Theme.zip** through **Appearance → Themes → Add New → Upload
Theme**. The outer deployment bundle is not the theme upload. Elementor remains
available to edit your pages; a WordPress theme ZIP uses the Themes screen.

Open **Settings → WiT Risk Intake** and enter:

- Forms intake URL: `https://forms.weinsurethings.com/integrations/wit/intake`
  (use the staging host during testing).
- Shared intake secret: the matching `WIT_INTAKE_SECRET` from step 3.

Save the connection. A blank password keeps the existing saved secret. The saved
secret is server-only and never shown in the form; Hedge credentials stay on the
forms server. Hosts can alternatively configure both bridge constants in
`wp-config.php`; those constants take priority over dashboard settings.

Use **Tools → WIT Page Setup → Create missing pages** with draft status selected,
or create `/submit-a-risk/` manually. Assign **Submit a Business Risk** as its
page template. Keep this template selected instead of Elementor Canvas. Add a
link or button from Elementor pages to `/submit-a-risk/` wherever shoppers should
enter the flow.

Exclude `/submit-a-risk/` and `/wp-json/wit/v1/specialty-risk` from full-page/CDN
caching. Confirm the site's contact information and privacy-policy link.

## 5. Verify and launch

With Hedge writes off, preview the draft intake page and submit synthetic business
details. A reference should appear only after the forms server saves the request.
Sign into `/hedge` and confirm the request appears in **Website requests**. A retry
of identical data/reference should produce one local draft. The consumer receipt
means received for agent review; it does not mean a quote or bound coverage.

Complete the staging checks in `docs/HEDGE-SETUP.md`, including staff submission,
PDF requirements, review/finalization, and signed webhook redelivery. Use a separate
database for production, configure its production credentials and webhook, and
verify connectivity before enabling `HEDGE_LIVE=1` and publishing the intake page.
The commercial and specialty hub buttons appear once configuration is complete
and the page is published. Elementor buttons can point to the same page.

To pause public intake, remove the saved connection using **Settings → WiT Risk
Intake → Pause intake** (or remove the server-managed secret if using constants).
Set `HEDGE_LIVE=0` and restart forms to pause outbound Hedge writes. Existing drafts
and event records remain in the database.

## Validation included

Automated checks use fake HTTP and synthetic data. They exercise receipt handling,
signatures, replay handling, scope/write gates, admin-only connection settings,
and installer backup/rollback. Browser checks cover desktop/mobile input, consent,
failure/retry, and success. They do not replace a staging check against your
installed WordPress, reverse proxy, service configuration, or Hedge account.

References: [WordPress theme upload](https://wordpress.org/documentation/article/appearance-themes-screen/),
[Hedge API documentation](https://docs.hedgespecialty.com/).
