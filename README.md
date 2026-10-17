# MediaRakshak AI — Firestore-backed prototype

**From digital abuse to documented action.** A victim-first workspace for organizing known post URLs, recording technical media properties, preserving original uploaded files in Firestore, and generating cautious response plans with Gemini.

## What this build stores

- **Firestore case documents** in project `media-rakshak-ai`, database `mr-db`, collection `cases`: case details, known URLs, evidence metadata, SHA-256 hashes, response plans, and user-entered link statuses.
- **Video/image file bytes** (supported video uploads in this build) in private Firestore subcollection documents under `cases/{case_id}/media/{upload_id}/chunks`. Firestore documents have a 1 MiB size limit, so the app splits files into Base64 chunks of 600 KiB raw data. A 50 MiB file uses roughly 86 chunk documents plus metadata. This works technically, but is less efficient and can cost more than Cloud Storage; Cloud Storage is the recommended production media store.
- **Explicit deletion control:** the selected case can be deleted, including its Firestore media metadata and chunks. This action is permanent.
- **No silent local database fallback.** If Firestore is unavailable, database API calls return HTTP 503 with a diagnostic hint. The app does not claim that data was saved if Firestore rejects the write.
- **Legacy JSON migration:** on startup, existing cases from `storage/cases.json` are imported only when their case ID does not already exist in Firestore. This is idempotent and does not overwrite existing Firestore cases. Legacy local video files are not uploaded automatically; upload those files through the app if you want them saved in Firestore.
- **Gemini:** uses `GEMINI_API_KEY` and `GEMINI_MODEL`. If no key is configured, the app returns a clearly labelled local fallback response plan.

## Important security note about `.env`

This ZIP includes `.env` and `.env.example` with **configuration variable names and safe placeholders**, not real API keys or service-account credentials. You must add your own Gemini API key locally if you want Gemini. Never email, commit, or share a populated `.env` or service-account JSON file. The ZIP you uploaded contained a populated Gemini key; if that was a real key, rotate it in Google AI Studio before using it again.

## Configure Firebase / Firestore

The app is preconfigured for:

- Project ID: `media-rakshak-ai`
- Database ID: `mr-db`
- Collection: `cases`

The backend uses Google Application Default Credentials (ADC) or the file path in `GOOGLE_APPLICATION_CREDENTIALS`. **No service-account key is bundled** because it is a credential and must remain private.

### Option A — use your existing service account JSON key

1. In Google Cloud Console, open **IAM & Admin → Service Accounts** and select your existing `mediarakshak-runtime` service account.
2. If your organization/project permits key creation, create and download a JSON key.
3. Store it locally in a `credentials` folder inside the project (do not add it to the ZIP or source control), e.g. `credentials/mediarakshak-runtime.json`.
4. Set `GOOGLE_APPLICATION_CREDENTIALS` in `.env` to the full Windows path, for example `C:\Users\YOUR_USER\Downloads\MediaRakshak_AI_Project\credentials\mediarakshak-runtime.json`.
5. Ensure the service account has the **Cloud Datastore User** role for this project. Do not make the database public.

### Option B — Application Default Credentials

If Google Cloud CLI is installed, run `gcloud auth application-default login` from a terminal and ensure the signed-in identity has access to Firestore. Leave `GOOGLE_APPLICATION_CREDENTIALS` blank in `.env`.

If the service account key is prohibited by policy, use ADC or ask the project administrator for an approved identity. Do not paste credentials into chat.

## Configure `.env`

The included `.env` already contains the keys. Open it in a text editor and fill only the values you need:

```dotenv
GEMINI_API_KEY=
GEMINI_MODEL=gemini-3.8-flash
FIRESTORE_PROJECT_ID=media-rakshak-ai
FIRESTORE_DATABASE_ID=mr-db
FIRESTORE_CASES_COLLECTION=cases
GOOGLE_APPLICATION_CREDENTIALS=
MIGRATE_LOCAL_CASES=true
MAX_UPLOAD_MB=50
```

A populated Gemini API key is optional for database operations. Firestore authentication is separate from the Gemini API key.

## Run on Windows PowerShell

Open PowerShell in the project folder (the folder containing `requirements.txt`):

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Alternatively, right-click `START_MEDIA_RAKSHAK.ps1` and run it with PowerShell (or execute it from a PowerShell window opened in the project folder). Open `http://127.0.0.1:8000`. The header should say **Firestore connected**. If it says Firestore unavailable, check `/api/health` for a safe error type, then verify the project ID, database ID, credentials, Firestore API, IAM role, and Google Cloud billing/API restrictions. The app cannot bypass cloud permission or billing requirements.

## Test suite

The tests use an in-memory Firestore fake, so they do not require cloud credentials and do not write to your real database:

```powershell
pytest -q
```

These tests validate case persistence behavior, URL validation, invalid link statuses, unsupported/empty/unreadable media rejection, chunked media round-tripping, media download integrity, response-plan fallback and grounding instructions for incomplete case details, case/media deletion, deployment-auth configuration, and graceful database errors. Live cloud integration still needs to be verified in your own project because this environment has no access to your Google Cloud account.

## Deployment readiness and Cloud Run

This repository includes a Dockerfile for Cloud Run. **Do not deploy it as an unprotected public app.** Case records and media are sensitive, and this prototype does not provide per-user accounts or multi-user authorization. For a controlled hackathon demo, enable the built-in HTTP Basic Auth before making the service reachable from the internet. Use a long, unique password and HTTPS (Cloud Run provides HTTPS).

### Required runtime settings

Set these in Cloud Run → Edit & deploy new revision → Variables & Secrets. Do not put secrets into the Docker image or source repository.

- `FIRESTORE_PROJECT_ID=media-rakshak-ai`
- `FIRESTORE_DATABASE_ID=mr-db`
- `FIRESTORE_CASES_COLLECTION=cases`
- `GEMINI_MODEL=gemini-3.8-flash` (use a model enabled for your API key/project)
- `SECURE_DEPLOYMENT=true`
- `APP_AUTH_USERNAME` and `APP_AUTH_PASSWORD`: set both. Prefer storing the password in Secret Manager and mounting/injecting it as an environment variable.
- `GEMINI_API_KEY`: set as a secret if Gemini is required.
- `GOOGLE_APPLICATION_CREDENTIALS`: **do not set a downloaded service-account JSON path in Cloud Run**. Attach a dedicated runtime service account to the Cloud Run service and grant it only the required Firestore role. The app then uses Application Default Credentials automatically.

The runtime service account must have access to the named database, and the project must have the Firestore API and billing configured as required. The health endpoint `/api/health` is intentionally reachable without Basic Auth for diagnostics; it returns configuration identifiers and status only. All other paths require Basic Auth when secure deployment is enabled. The browser should show a sign-in prompt when you open the app.

### Before deploying

1. Run the automated tests: `pytest -q`.
2. Confirm `/api/health` reports `firestore: connected` from your local environment.
3. Test create → restart → reload; add a known URL; analyze a synthetic sample; download it and verify the SHA-256; generate a plan with missing fields; export JSON; delete the test case.
4. Confirm the JSON export contains the case details, link records, evidence hashes/metadata, and saved response plan, but never raw video bytes or credential material.
5. Set a budget alert and review Firestore write/read/storage costs. Storing media chunks directly in Firestore is a prototype constraint, not a recommended long-term media architecture.
6. Deploy a revision, verify health, sign in, and rerun the synthetic demo against the deployed URL. Do not assume local tests prove cloud IAM, billing, or live Gemini behavior.

### Build/run container locally

```powershell
docker build -t mediarakshak-ai .
docker run --rm -p 8080:8080 -e PORT=8080 mediarakshak-ai
```

For a deployment, provide credentials through the runtime identity and secrets through the hosting platform, not via a baked-in `.env` file. The included `.dockerignore` excludes `.env`, credentials, and local uploads from the build context.

## Prototype safeguards / limitations

- Technical metadata and hashes are **not** a deepfake detector and do not prove authenticity. The app marks authenticity as `UNVERIFIED`.
- Media is stored as chunked Firestore documents only because you requested Firestore-only persistence. This is technically possible but inefficient; for production, use a private Cloud Storage bucket and store only its object path and metadata in Firestore.
- Firestore and Gemini usage can incur charges or be blocked by billing, API, IAM, or quota settings. This app cannot bypass those controls.
- The optional HTTP Basic Auth is a minimum demo gate, not a replacement for proper identity, per-user authorization, audit controls, or a production security review. Do not use real intimate/abusive content in this prototype. Use synthetic or explicitly authorized test media.
- The app does not search all platforms, submit complaints, or remove content. A human must review every AI-generated draft and verify link statuses.
- For live/public deployment, add authentication, authorization, rate limits, retention/deletion controls, audit logging, and a privacy/security review.

## Demo flow

1. Create a synthetic incident case.
2. Add a known post URL (use an example URL only).
3. Upload one of the supplied synthetic clips and optionally a reference clip.
4. Show file metadata, SHA-256, `UNVERIFIED` status, and the download button for the Firestore-stored original.
5. Generate a Gemini response plan and review the complaint draft.
6. Change a link status only after verifying it, then export the case JSON.
