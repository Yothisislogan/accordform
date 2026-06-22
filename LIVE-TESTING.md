# WIT Forms — Live Testing Checklist

This checklist is for the first real app-flow test using Logan's licensed ACORD PDFs.

Licensed PDFs must stay out of git. Put them only on the machine/server running WIT Forms.

## 1. Install requirements

```bash
pip install -r requirements.txt
sudo apt-get update
sudo apt-get install -y pdftk
```

## 2. Put licensed source PDFs on the server

Place the original fillable licensed PDFs under:

```text
templates/acord/source/
```

Example source files:

```text
templates/acord/source/ACORD_25.pdf
templates/acord/source/ACORD_28.pdf
templates/acord/source/ACORD_35.pdf
templates/acord/source/ACORD_36.pdf
templates/acord/source/ACORD_125.pdf
templates/acord/source/ACORD_126.pdf
templates/acord/source/ACORD_127.pdf
templates/acord/source/ACORD_128.pdf
templates/acord/source/ACORD_130.pdf
templates/acord/source/ACORD_131.pdf
templates/acord/source/ACORD_135_NC.pdf
templates/acord/source/ACORD_140.pdf
templates/acord/source/ACORD_141.pdf
```

## 3. Prep clean templates

Run `tools/prep_template.py` once for each source PDF. The clean output name must match the catalog convention:

```bash
python tools/prep_template.py templates/acord/source/ACORD_25.pdf -o templates/acord/ACORD_25_clean.pdf
python tools/prep_template.py templates/acord/source/ACORD_28.pdf -o templates/acord/ACORD_28_clean.pdf
python tools/prep_template.py templates/acord/source/ACORD_35.pdf -o templates/acord/ACORD_35_clean.pdf
python tools/prep_template.py templates/acord/source/ACORD_36.pdf -o templates/acord/ACORD_36_clean.pdf
python tools/prep_template.py templates/acord/source/ACORD_125.pdf -o templates/acord/ACORD_125_clean.pdf
python tools/prep_template.py templates/acord/source/ACORD_126.pdf -o templates/acord/ACORD_126_clean.pdf
python tools/prep_template.py templates/acord/source/ACORD_127.pdf -o templates/acord/ACORD_127_clean.pdf
python tools/prep_template.py templates/acord/source/ACORD_128.pdf -o templates/acord/ACORD_128_clean.pdf
python tools/prep_template.py templates/acord/source/ACORD_130.pdf -o templates/acord/ACORD_130_clean.pdf
python tools/prep_template.py templates/acord/source/ACORD_131.pdf -o templates/acord/ACORD_131_clean.pdf
python tools/prep_template.py templates/acord/source/ACORD_135_NC.pdf -o templates/acord/ACORD_135_NC_clean.pdf
python tools/prep_template.py templates/acord/source/ACORD_140.pdf -o templates/acord/ACORD_140_clean.pdf
python tools/prep_template.py templates/acord/source/ACORD_141.pdf -o templates/acord/ACORD_141_clean.pdf
```

## 4. Run unit/schema tests

```bash
pytest -q
```

Expected result: all tests pass, except the live ACORD 25 fill test may skip if `pdftk` or the clean template is not available.

## 5. Run live PDF smoke tests

Run all forms:

```bash
python tools/live_smoke_test.py
```

Run one form:

```bash
python tools/live_smoke_test.py --form 125
```

Smoke-test outputs are written to:

```text
data/smoke_outputs/
```

Open each output and check that the values render on the real ACORD pages.

## 6. Start the app

```bash
python app.py
```

Open:

```text
http://127.0.0.1:8097
```

For production, use gunicorn behind nginx:

```bash
gunicorn -w 3 -b 127.0.0.1:8097 "app:create_app()"
```

## 7. Manual app-flow test

Test these in order:

1. Search for ACORD 25.
2. Open it.
3. Fill core fields.
4. Preview PDF.
5. Download PDF.
6. Print PDF.
7. Use my email — this should download the flattened PDF so the user can attach it from their own email client.

Then repeat the same flow for:

```text
ACORD 125
ACORD 126
ACORD 140
ACORD 127
ACORD 130
ACORD 135 NC
ACORD 141
ACORD 28
ACORD 35
ACORD 36
```

## 8. Phase 1 pass/fail criteria

A form passes Phase 1 when:

- It appears in search.
- It opens without schema errors.
- Required fields validate.
- Preview returns a flattened PDF.
- Download returns a flattened PDF.
- Print opens the generated PDF.
- Use my email downloads the generated PDF.
- The generated PDF has values in the correct fields.
- No editable/unflattened form is sent or downloaded as the final output.

## 9. Known Phase 2 work

Do not block Phase 1 on these:

- NowCerts lookup.
- Draft saving.
- Admin field re-tagging UI.
- Native SMTP/SendGrid email.
- Companion-form launch buttons from ACORD 125.
- Full expansion of every rare/repeated field on large forms.
