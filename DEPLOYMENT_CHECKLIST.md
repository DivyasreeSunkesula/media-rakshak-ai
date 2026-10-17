# MediaRakshak AI — Deployment Checklist

Use this checklist before treating the prototype as deployment-ready. Passing offline tests does not prove live Google Cloud IAM, billing, quotas, or Gemini availability.

## A. Local pre-deployment gate

- [ ] `pytest -q` passes.
- [ ] `python -m py_compile app/main.py tests/test_app.py` passes.
- [ ] `http://127.0.0.1:8000/api/health` reports `firestore: connected`.
- [ ] Create a case with intentionally incomplete notes; restart FastAPI; confirm it remains visible.
- [ ] Add `https://example.com/synthetic-demo` to that case; confirm it is attached to the correct case.
- [ ] Analyze `sample_videos/synthetic_variant.mp4` and optionally `synthetic_reference.mp4`.
- [ ] Download each stored original and compare its SHA-256 with the displayed hash.
- [ ] Try a text file renamed to `.mp4`; the app must show a readable-video error and create no evidence record.
- [ ] Generate a Gemini plan with no URL, account, or date. Check that those details remain unknown/placeholders and are not invented.
- [ ] Export JSON and verify case, links, evidence metadata/hashes, and response plan are present. Raw media bytes and credentials must not be included.
- [ ] Delete the synthetic test case and verify its case document, media metadata, and chunks are gone from Firestore.

## B. Cloud Run security gate

- [ ] Use a dedicated Cloud Run runtime service account with only the necessary Firestore access.
- [ ] Enable Firestore API and confirm the named database `mr-db` is accessible by that runtime identity.
- [ ] Set `FIRESTORE_PROJECT_ID=media-rakshak-ai`, `FIRESTORE_DATABASE_ID=mr-db`, and `FIRESTORE_CASES_COLLECTION=cases`.
- [ ] Set `SECURE_DEPLOYMENT=true`.
- [ ] Set `APP_AUTH_USERNAME` and a long, unique `APP_AUTH_PASSWORD`; store the password and Gemini key in Secret Manager.
- [ ] Do not upload a service-account JSON key to Cloud Run. Use the attached runtime identity / Application Default Credentials.
- [ ] Confirm `.env`, `credentials/`, and local uploads are excluded from the Docker build context.
- [ ] Set budget alerts and review Firestore costs. Media stored as Firestore chunks can be expensive; Cloud Storage is the recommended long-term design.
- [ ] Do not use real intimate/abusive content in this prototype. It lacks per-user accounts, user-level authorization, full audit trails, and a formal retention policy.

## C. Post-deployment smoke test

- [ ] Open `/api/health`; confirm Firestore is connected.
- [ ] Open the app URL; confirm the Basic Auth prompt appears and unauthenticated access is rejected.
- [ ] Sign in and repeat create → link → analyze synthetic clip → download/hash verify → Gemini plan → export → delete.
- [ ] Review Cloud Run logs for errors and check Firestore for the expected documents.
- [ ] If any step fails, do not use real incident data or claim the deployment is validated until resolved.
