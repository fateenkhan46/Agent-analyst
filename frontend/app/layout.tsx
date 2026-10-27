import type { Metadata } from "next";
import "./globals.css";
import { AppShell } from "@/components/shell";

export const metadata: Metadata = {
  title: "Power BI Agent",
  description:
    "An agent workforce that profiles source data, writes minutes of meeting, " +
    "designs a semantic model, generates validated DAX and exports an editable " +
    "Power BI project.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <AppShell>{children}</AppShell>
      </body>
    </html>
  );
}
