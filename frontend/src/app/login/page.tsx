"use client";

import { useRouter } from "next/navigation";
import Link from "next/link";
import { SignIn } from "@/components/SignIn";

/** Sign in. A route rather than a state inside the shell, so it can be
 *  linked, bookmarked and redirected to. */
export default function LoginPage() {
  const router = useRouter();
  return (
    <SignIn
      onSignedIn={() => router.replace("/dashboard")}
      footer={
        <>
          Not part of an organisation yet?{" "}
          <Link href="/signup">How access works</Link>
        </>
      }
    />
  );
}
