import type { Metadata } from "next";
import Link from "next/link";
import { Inter } from "next/font/google";
import "./globals.css";

const inter = Inter({
  subsets: ["latin"],
  variable: "--font-sans",
  display: "swap",
});

export const metadata: Metadata = {
  title: "Concierge",
  description:
    "An autonomous travel agent that researches, compares, and presents three options behind a strict two step approval gate.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={inter.variable}>
      <body className="font-sans">
        <header className="glass-nav sticky top-0 z-50" style={{ height: "var(--nav-h, 48px)" }}>
          <div className="mx-auto max-w-[980px] px-6 h-full flex items-center justify-between">
            <Link
              href="/"
              className="text-[15px] font-medium text-[var(--text)] tracking-tight"
            >
              Concierge
            </Link>
            <nav className="flex items-center gap-7 text-[12px] text-[var(--text-soft)]">
              <Link href="/" className="hover:text-[var(--text)] transition-colors">
                Plan
              </Link>
              <Link href="/audit" className="hover:text-[var(--text)] transition-colors">
                Audit
              </Link>
            </nav>
          </div>
        </header>
        <main>{children}</main>
      </body>
    </html>
  );
}
