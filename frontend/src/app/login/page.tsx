"use client";

import { useRouter } from "next/navigation";
import { AuthCard } from "@/components/AuthCard";
import { SignIn } from "@/components/SignIn";

export default function LoginPage() {
  const router = useRouter();
  return (
    <AuthCard
      heading="Sign in"
      blurb="Access your organisation's invoices."
      footer={{
        text: "No access yet?",
        href: "/signup",
        label: "Request access",
      }}
    >
      <SignIn onSignedIn={() => router.replace("/dashboard")} />
    </AuthCard>
  );
}
