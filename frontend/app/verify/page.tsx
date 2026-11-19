import { Suspense } from "react";
import type { Metadata } from "next";
import VerifyRouteContent from "./route-content";

export const metadata: Metadata = {
  title: "Verify your email | SkillBridge",
  description: "Confirm your SkillBridge waitlist email address.",
  robots: {
    index: false,
    follow: false,
  },
};

type VerifyPageProps = {
  searchParams: Promise<{
    token?: string | string[];
  }>;
};

function VerifyLoading() {
  return (
    <div
      role="status"
      aria-live="polite"
      className="glass-card mx-auto w-full max-w-2xl rounded-2xl p-9 text-center"
    >
      <p className="font-mono text-xs tracking-widest text-cyan-400">
        SKILLBRIDGE // IDENTITY
      </p>
      <p className="mt-6 text-sm text-zinc-400">
        Loading verification protocol...
      </p>
    </div>
  );
}

export default function VerifyPage({ searchParams }: VerifyPageProps) {
  return (
    <main className="relative flex min-h-screen items-center justify-center overflow-hidden bg-[#050505] px-5 py-16 text-white">
      <div className="grid-bg opacity-30" />
      <div className="orb orb-blue" />
      <div className="orb orb-purple" />

      <div className="relative z-10 w-full">
        <Suspense fallback={<VerifyLoading />}>
          <VerifyRouteContent searchParams={searchParams} />
        </Suspense>
      </div>
    </main>
  );
}
