import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "AutoFlow Trader — Deploy Algorithmic Strategies Safely",
  description:
    "Broker-safe automated strategy deployment platform. Kill switch, reconciliation, incident logging, heartbeat monitoring, paper smoke tests, VPS deployment.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className="dark">
      <body className="min-h-screen bg-background text-foreground antialiased">{children}</body>
    </html>
  );
}
