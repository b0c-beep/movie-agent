import type { Metadata } from "next";

import "./styles.css";

export const metadata: Metadata = {
  title: "Movie Agent",
  description: "Conversational, grounded movie recommendations",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
