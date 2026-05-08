# Web UI (M4)

Next.js 15 (App Router) + Tailwind. Adds:

- `/` — chat surface (POST /v1/trips)
- `/trip/[id]` — three option cards + ApprovalDrawer
- `/audit` — live event timeline + client-side hash-chain verification

Run after M3 is solid:

```bash
cd web
npm install
npm run dev   # → http://localhost:3000
```

The drawer's two-tap UX:

1. **Tap 1** — selection: POSTs to /select, server runs revalidation, drawer renders fresh consent text + drift diff if any.
2. **Tap 2** — authorization: navigator.credentials.get() with server-issued challenge bound to option_id; server mints ApprovalToken; client posts to /approve.

WebAuthn implementation: `approval/webauthn.py` (server) + `@simplewebauthn/browser` (client).
