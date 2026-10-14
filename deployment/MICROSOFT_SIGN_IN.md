# Connecting a customer's Microsoft 365

One OIDC provider per customer Entra tenant. Policy: SECURITY.md, "Sign-in".

1. **From the customer:** their Entra tenant id (Entra → Overview) and the
   email domain their people sign in with.
2. **Entra app registration** (the operator's, multi-tenant or one per
   customer): redirect URI `https://<auth-domain>/__/auth/handler`, a client
   secret, and the customer's admin consent if their tenant requires it.
3. **Identity Platform → Providers → Add a provider → OpenID Connect:**
   name `entra-<customer>` (its id becomes `oidc.entra-<customer>`),
   issuer `https://login.microsoftonline.com/<tenant id>/v2.0`, the app's
   client id and secret, **code flow**.
4. **Bind:**

       python -m invoice_processing.admin.provision bind-microsoft \
           --tenant <slug> --domain <domain> --provider oidc.entra-<customer> \
           --entra-tenant <tenant id> --role clerk --operator <you>

5. **Verify:** `GET /api/sso/microsoft?email=someone@<domain>` returns the
   provider id; a person from that tenant signs in with "Sign in with
   Microsoft" and lands in the tenant as the default role. The frontend
   shows the button only with `NEXT_PUBLIC_SSO_MICROSOFT=1` (a build-time
   setting).

To disconnect: the tenant's admin removes the binding on the Admin page, or
the operator deletes the provider (which stops every sign-in through it).
