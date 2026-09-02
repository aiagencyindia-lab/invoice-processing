import type { Metadata } from "next";
import { AppShell } from "@/components/AppShell";
import "./globals.css";

export const metadata: Metadata = {
  // The tab is where someone finds this window among twenty others, so it
  // carries the product name rather than a description of the category.
  title: "Ledgerly",
  description:
    "Auditable accounts-payable review by AIxAI: every decision traceable "
    + "to a written rule.",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body>
        <AppShell>{children}</AppShell>
      </body>
    </html>
  );
}
