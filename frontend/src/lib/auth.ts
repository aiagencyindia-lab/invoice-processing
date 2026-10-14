/** Identity Platform sign-in.
 *
 *  The API verifies a Firebase ID token on every request with
 *  `id_token.verify_firebase_token(..., audience=GOOGLE_CLOUD_PROJECT)`, so
 *  the browser's job is only to obtain one and keep it fresh. There is no
 *  session cookie and no login endpoint: a bearer token the server checks
 *  against Google's rotating keys is the whole mechanism.
 *
 *  The SDK is imported dynamically so its weight lands only when someone
 *  actually signs in, and never at all on a deployment running with
 *  authentication disabled.
 */

const CONFIG = {
  apiKey: process.env.NEXT_PUBLIC_FIREBASE_API_KEY,
  authDomain: process.env.NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN,
  projectId: process.env.NEXT_PUBLIC_FIREBASE_PROJECT_ID,
  appId: process.env.NEXT_PUBLIC_FIREBASE_APP_ID,
};

/** Whether the browser half is configured. The server decides whether a token
 *  is *required*; this only says whether we are able to obtain one. */
export function firebaseConfigured(): boolean {
  return Boolean(CONFIG.apiKey && CONFIG.authDomain && CONFIG.projectId);
}

export type SignedInUser = { email: string | null; uid: string };

type FirebaseAuth = Awaited<typeof import("firebase/auth")>;
type AuthInstance = ReturnType<FirebaseAuth["getAuth"]>;

let cached: Promise<{ mod: FirebaseAuth; auth: AuthInstance }> | null = null;

async function client() {
  if (!cached) {
    cached = (async () => {
      const [{ initializeApp, getApps }, mod] = await Promise.all([
        import("firebase/app"),
        import("firebase/auth"),
      ]);
      const app = getApps().length
        ? getApps()[0]
        : initializeApp(CONFIG as Record<string, string>);
      return { mod, auth: mod.getAuth(app) };
    })();
  }
  return cached;
}

/** Firebase error codes worth explaining rather than echoing raw. */
function explain(code: string): string | null {
  switch (code) {
    case "auth/unauthorized-domain":
      return "This address is not in the project's authorised domains, so sign-in was blocked.";
    case "auth/popup-blocked":
      return "Your browser blocked the sign-in window. Allow pop-ups for this site and try again.";
    case "auth/popup-closed-by-user":
    case "auth/cancelled-popup-request":
      return "Sign-in was cancelled.";
    case "auth/network-request-failed":
      return "Could not reach the sign-in service — check your connection and try again.";
    case "auth/email-already-in-use":
      return "That address already has an account. Sign in instead, or reset the password.";
    case "auth/invalid-email":
      return "That does not look like an email address.";
    case "auth/weak-password":
      return "Passwords need at least six characters.";
    case "auth/invalid-credential":
    case "auth/wrong-password":
    case "auth/user-not-found":
      // Deliberately one message for all three: saying which half was wrong
      // tells an attacker whether an address has an account here.
      return "That email and password do not match an account.";
    case "auth/too-many-requests":
      return "Too many attempts. Wait a few minutes, or reset the password.";
    case "auth/operation-not-allowed":
      return "Email and password sign-in is not switched on for this project yet — enable it under Authentication → Sign-in method.";
    default:
      return null;
  }
}

export class SignInError extends Error {}

function codeOf(caught: unknown): string {
  return typeof caught === "object" && caught !== null && "code" in caught
    ? String((caught as { code: unknown }).code)
    : "";
}

function asSignInError(caught: unknown): SignInError {
  return new SignInError(
    explain(codeOf(caught)) ??
      (caught instanceof Error ? caught.message : String(caught)),
  );
}

/** Codes that mean "the popup never opened", as opposed to "the user changed
 *  their mind". Only these are worth retrying by another route. */
const POPUP_UNAVAILABLE = new Set([
  "auth/popup-blocked",
  "auth/cancelled-popup-request",
  "auth/operation-not-supported-in-this-environment",
  "auth/web-storage-unsupported",
]);

async function signInWith(
  make: (mod: FirebaseAuth) => import("firebase/auth").AuthProvider,
): Promise<void> {
  const { mod, auth } = await client();
  const provider = make(mod);
  try {
    await mod.signInWithPopup(auth, provider);
  } catch (caught) {
    if (!POPUP_UNAVAILABLE.has(codeOf(caught))) throw asSignInError(caught);
    // The popup could not open -- an extension, a mobile browser, a corporate
    // policy. Redirecting navigates in this same tab, which nothing blocks.
    // It is the fallback rather than the default because it round-trips
    // through the Firebase auth domain, and a browser partitioning
    // third-party storage can drop the result on the way back.
    await mod.signInWithRedirect(auth, provider);
  }
}

export async function signInWithGoogle(): Promise<void> {
  await signInWith((mod) => new mod.GoogleAuthProvider());
}

/** Through the OIDC provider created for this customer's Entra tenant;
 *  the hint saves the person retyping their address at Microsoft. */
export async function signInWithMicrosoft(
  providerId: string,
  email: string,
): Promise<void> {
  await signInWith((mod) => {
    const provider = new mod.OAuthProvider(providerId);
    provider.setCustomParameters({ login_hint: email });
    return provider;
  });
}

/** Report a redirect sign-in that came back and failed.
 *
 *  A redirect result arrives on page load, not from the click, so an error
 *  raised during it has nobody waiting to catch it. Without this the user
 *  returns to an unchanged sign-in screen with no explanation of what
 *  happened -- which looks exactly like a button that does nothing.
 */
export async function redirectSignInError(): Promise<string | null> {
  if (!firebaseConfigured()) return null;
  try {
    const { mod, auth } = await client();
    await mod.getRedirectResult(auth);
    return null;
  } catch (caught) {
    return asSignInError(caught).message;
  }
}

export async function signOut(): Promise<void> {
  const { mod, auth } = await client();
  await mod.signOut(auth);
}

/** The current ID token, or null when nobody is signed in.
 *
 *  `getIdToken()` returns the cached token and mints a fresh one when it is
 *  close to expiry, which is why every request asks for it rather than
 *  holding one in a variable -- a token stored at sign-in is stale an hour
 *  later and every request after that would be a 401.
 */
export async function currentIdToken(): Promise<string | null> {
  if (!firebaseConfigured()) return null;
  const { auth } = await client();
  return auth.currentUser ? auth.currentUser.getIdToken() : null;
}

/** Subscribe to sign-in state. Returns an unsubscribe function. */
export async function watchUser(
  onChange: (user: SignedInUser | null) => void,
): Promise<() => void> {
  if (!firebaseConfigured()) {
    onChange(null);
    return () => {};
  }
  const { mod, auth } = await client();
  return mod.onIdTokenChanged(auth, (user) =>
    onChange(user ? { email: user.email, uid: user.uid } : null),
  );
}

/** Email and password.
 *
 *  Firebase mints the same kind of ID token for a password sign-in as for a
 *  Google one, and the API verifies it the same way -- so this adds an
 *  authentication method, not a second authentication system. Nothing on the
 *  server changes.
 */
export async function signInWithPassword(
  email: string,
  password: string,
): Promise<void> {
  const { mod, auth } = await client();
  try {
    await mod.signInWithEmailAndPassword(auth, email, password);
  } catch (caught) {
    throw asSignInError(caught);
  }
}

export async function signUpWithPassword(
  email: string,
  password: string,
): Promise<void> {
  const { mod, auth } = await client();
  try {
    const created = await mod.createUserWithEmailAndPassword(
      auth,
      email,
      password,
    );
    // The API refuses a token whose email is unverified, so the message has
    // to go out as part of signing up rather than being left for later.
    await mod.sendEmailVerification(created.user);
  } catch (caught) {
    throw asSignInError(caught);
  }
}

export async function sendPasswordReset(email: string): Promise<void> {
  const { mod, auth } = await client();
  try {
    await mod.sendPasswordResetEmail(auth, email);
  } catch (caught) {
    throw asSignInError(caught);
  }
}

/** Re-send verification to the account that is signed in. */
export async function resendVerification(): Promise<void> {
  const { mod, auth } = await client();
  if (!auth.currentUser) throw new SignInError("Nobody is signed in.");
  await mod.sendEmailVerification(auth.currentUser);
}

export async function isEmailVerified(): Promise<boolean> {
  if (!firebaseConfigured()) return false;
  const { auth } = await client();
  if (!auth.currentUser) return false;
  await auth.currentUser.reload();
  return auth.currentUser.emailVerified;
}

/** Pull a fresh ID token, so claims written a moment ago are actually in it.
 *
 *  Custom claims are baked in when the token is minted. Without forcing a
 *  refresh, an account that has just been granted a tenant keeps presenting
 *  the token it signed up with -- which has no claims -- and is refused for up
 *  to an hour by a system that has already granted it access.
 */
export async function refreshToken(): Promise<void> {
  if (!firebaseConfigured()) return;
  const { auth } = await client();
  if (auth.currentUser) await auth.currentUser.getIdToken(true);
}

/** Whether the signed-in token already carries a tenant claim. True when
 *  Firebase is not configured, so the dev bypass never triggers a refresh. */
export async function tokenHasTenant(): Promise<boolean> {
  if (!firebaseConfigured()) return true;
  const { auth } = await client();
  if (!auth.currentUser) return true;
  const result = await auth.currentUser.getIdTokenResult();
  return !!result.claims.tenant_id;
}
