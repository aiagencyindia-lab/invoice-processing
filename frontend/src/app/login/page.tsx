"use client";

import { useRouter } from "next/navigation";
import { AuthCard, OrDivider } from "@/components/AuthCard";
import { PasswordForm } from "@/components/PasswordForm";
import { SignIn } from "@/components/SignIn";
import { firebaseConfigured } from "@/lib/auth";

export default function LoginPage() {
  const router = useRouter();
  const done = () => router.replace("/dashboard");

  return (
    <AuthCard
      heading="Sign in"
      blurb="Access your organisation's invoices."
      footer={{ text: "No account yet?", href: "/signup", label: "Create one" }}
    >
      <SignIn onSignedIn={done} />
      {firebaseConfigured() ? (
        <>
          <OrDivider />
          <PasswordForm mode="login" onSignedIn={done} onSignedUp={() => {}} />
        </>
      ) : null}
    </AuthCard>
  );
}
