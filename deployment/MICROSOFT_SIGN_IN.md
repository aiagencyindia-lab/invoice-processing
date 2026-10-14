# Connecting a customer's Microsoft 365

One OIDC provider per customer Entra tenant. Policy: SECURITY.md, "Sign-in".

1. **From the customer:** their Entra tenant id (Entra → Overview) and the
   email domain their people sign in with.
2. **Entra app registration** (the operator's, multi-tenant or one per
   customer): redirect URI `https://<auth-domain>/__/auth/handler`, a client
   secret, and the customer's admin consent if their tenant requires it. The
   id token must carry the `email` claim (Token configuration → Add optional
   claim → ID → `email`, and the `email` scope); without it every sign-in is
   refused with "Token carries no email claim."
3. **Identity Platform → Providers → Add a provider → OpenID Connect:**
   name `entra-<customer>` in lower-case letters, digits and hyphens only
   (its id becomes `oidc.entra-<customer>`; `bind-microsoft` accepts only
   `oidc.` plus 1-40 such characters), issuer
   `https://login.microsoftonline.com/<tenant id>/v2.0`, the app's client
   id and secret, **code flow**. One provider per customer: `bind-microsoft`
   refuses a provider another tenant already trusts.
4. **Bind:**

       python -m invoice_processing.admin.provision bind-microsoft \
           --tenant <slug> --domain <domain> --provider oidc.entra-<customer> \
           --entra-tenant <tenant id> --role clerk --operator <you>

5. **Verify:** `curl -X POST <app>/api/sso/microsoft -H 'Content-Type:
   application/json' -d '{"email": "someone@<domain>"}'` returns the
   provider id; a person from that tenant signs in with "Sign in with
   Microsoft" and lands in the tenant as the default role. The frontend
   shows the button only with `NEXT_PUBLIC_SSO_MICROSOFT=1` (a build-time
   setting).

To disconnect: the tenant's admin removes the binding on the Admin page, or
the operator deletes the provider (which stops every sign-in through it).
